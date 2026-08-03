// Scoring trigger + persist — port of the Railway path traced in
// backend/services/scoring_service.py's docstring:
//   POST /api/{team}/score        → prefetch transcript+details (app-side,
//     avoids Dialpad bursts), CC grounding (mode=on — DispositionDesign §5),
//     build all prompts, trigger the qa-scoring-pipeline workflow, persist a
//     job row (idempotent on (team, call_id, agent)).
//   POST /api/v1/callbacks/qa-scoring-pipeline → validate the scorecard
//     against config, write the draft evaluation + sections, stamp active
//     formula/rubric versions, evaluate the formula, quantize, run the
//     §3.14 human-review gate → auto-finalize (+ qa_events eval_approved
//     toast) or park flagged_human_review.
//   GET /api/{team}/score/{job_id} → job status (D1-backed, not in-memory —
//     an improvement over Railway's restart-forgetting _jobs store).

import { loadTeamConfig, type TeamConfig } from "../lib/teamConfig.js";
import { fetchCallContext, buildCallContextBlock } from "../lib/ccContext.js";
import {
  ANNOTATOR_RESPONSE_SCHEMA,
  ANNOTATOR_SYSTEM,
  buildAnnotatorPrompt,
  buildJudgePromptTemplate,
  buildJudgeSystemPrompt,
  buildLongCallNote,
  buildScoringPrompt,
  buildSystemPrompt,
} from "../lib/scoringPrompts.js";
import { evaluateFormula, quantizeScore } from "../lib/ruleEngine.js";
import { fetchSopContext } from "../lib/sopRetrieval.js";

const WORKFLOW_NAME = "qa-scoring-pipeline";
const DP = "https://dialpad.com/api/v2";

const json = (data: unknown, status = 200) =>
  new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json" },
  });

// ── Dialpad prefetch (ports of get_transcript / get_call_details) ───────────

function mmssFrom(callStart: number | null, tsRaw: string): string {
  if (!tsRaw || callStart === null) return "";
  const t = Date.parse(tsRaw);
  if (Number.isNaN(t)) return "";
  const elapsed = Math.trunc((t - callStart) / 1000);
  return `${Math.trunc(elapsed / 60)}:${String(elapsed % 60).padStart(2, "0")}`;
}

async function getTranscript(key: string, callId: string) {
  const empty = { transcript_text: "", transcript_display: [] as any[], moments_display: [] as any[] };
  try {
    const r = await fetch(`${DP}/transcripts/${encodeURIComponent(callId)}`, {
      headers: { authorization: `Bearer ${key}` },
    });
    if (!r.ok) return empty;
    const data = (await r.json()) as any;
    const transcriptLines: string[] = [];
    const transcriptDisplay: any[] = [];
    const momentsDisplay: any[] = [];
    let callStart: number | null = null;
    for (const line of data.lines ?? []) {
      const tsRaw = line.time ?? "";
      if (line.type === "transcript") {
        const name = line.name ?? "Unknown";
        const content = (line.content ?? "").trim();
        if (!content) continue;
        if (tsRaw && callStart === null) {
          const t = Date.parse(tsRaw);
          if (!Number.isNaN(t)) callStart = t;
        }
        transcriptLines.push(`${name}: ${content}`);
        transcriptDisplay.push({
          timestamp: mmssFrom(callStart, tsRaw),
          speaker: name,
          text: content,
        });
      } else if (["moment", "real_time_moment", "custom_moment"].includes(line.type)) {
        const momentType = line.moment_type || (line.content ?? "").trim();
        if (!momentType) continue;
        momentsDisplay.push({
          timestamp: mmssFrom(callStart, tsRaw),
          time: tsRaw,
          type: momentType,
          agent: line.name ?? "",
        });
      }
    }
    return {
      transcript_text: transcriptLines.join("\n"),
      transcript_display: transcriptDisplay,
      moments_display: momentsDisplay,
    };
  } catch {
    return empty;
  }
}

async function getCallDetails(key: string, callId: string) {
  try {
    const r = await fetch(`${DP}/call/${encodeURIComponent(callId)}`, {
      headers: { authorization: `Bearer ${key}` },
    });
    if (!r.ok) return {} as any;
    const raw = (await r.json()) as any;
    const contact = raw.contact ?? {};
    return {
      caller_name: contact.name ?? "",
      caller_phone: contact.phone ?? "",
      caller_email: contact.email ?? "",
      date_connected: raw.date_connected ?? "",
      date_ended: raw.date_ended ?? "",
      total_duration: raw.total_duration ?? raw.duration ?? 0,
      entry_point_call_id: String(raw.entry_point_call_id ?? ""),
      master_call_id: String(raw.master_call_id ?? ""),
      was_recorded: !!raw.recording_details,
      raw,
    } as any;
  } catch {
    return {} as any;
  }
}

const epochToIso = (v: any): string | null => {
  const n = Number(v);
  return v && Number.isFinite(n) ? new Date(n).toISOString() : null;
};

// ── trigger ────────────────────────────────────────────────────────────────

export async function scoreTrigger(
  request: Request,
  db: D1Database,
  teamId: string,
  env: {
    DIALPAD_API_KEY?: string;
    PULPO_MCP_URL?: string;
    PULPO_MCP_TOKEN?: string;
  }
): Promise<Response> {
  let form: FormData;
  try {
    form = await request.formData();
  } catch {
    return json({ detail: "multipart/form-data body required" }, 422);
  }
  return scoreTriggerInternal(request, db, teamId, env, {
    callId: (form.get("call_id") ?? "").toString().trim(),
    agentEmail: (form.get("agent_email") ?? "").toString().trim().toLowerCase(),
    managerEmail: (form.get("manager_email") ?? "").toString().trim().toLowerCase(),
  });
}

async function scoreTriggerInternal(
  request: Request,
  db: D1Database,
  teamId: string,
  env: {
    DIALPAD_API_KEY?: string;
    PULPO_MCP_URL?: string;
    PULPO_MCP_TOKEN?: string;
  },
  opts: { callId: string; agentEmail: string; managerEmail: string; rescoreOf?: number }
): Promise<Response> {
  if (!env.DIALPAD_API_KEY)
    return json({ detail: "DIALPAD_API_KEY app secret not configured" }, 503);
  const { callId, agentEmail, managerEmail } = opts;
  if (!callId || !agentEmail || !managerEmail)
    return json({ detail: "call_id, agent_email, manager_email required" }, 422);

  // identity: roster resolve (agent must be on THIS team's roster)
  const agentRow = await db
    .prepare(
      "SELECT id, name, canonical_name, email FROM qa_agents WHERE team_id = ? AND active = 1 AND LOWER(email) = ? LIMIT 1"
    )
    .bind(teamId, agentEmail)
    .first<any>();
  if (!agentRow)
    return json({ detail: `agent ${agentEmail} not on the ${teamId} roster` }, 403);
  const agentName = (agentRow.canonical_name || agentRow.name).trim();

  // idempotency: one active job per (team, call, agent)
  const jobId = `score-${teamId}-${callId}-${agentName.toLowerCase().replace(/[^a-z0-9]+/g, "-")}`;
  const existing = await db
    .prepare("SELECT run_id, status FROM workflow_runs WHERE run_id = ?")
    .bind(jobId)
    .first<any>();
  if (existing && ["pending", "running"].includes(existing.status))
    return json({ job_id: jobId, status: existing.status, deduped: true });
  if (!opts.rescoreOf) {
    const already = await db
      .prepare(
        "SELECT id FROM qa_evaluations WHERE team_id = ? AND dialpad_call_id = ? LIMIT 1"
      )
      .bind(teamId, callId)
      .first<any>();
    if (already)
      return json({ detail: `call ${callId} already has evaluation ${already.id}` }, 409);
  }

  const config = await loadTeamConfig(db, teamId);
  const [transcript, details] = await Promise.all([
    getTranscript(env.DIALPAD_API_KEY, callId),
    getCallDetails(env.DIALPAD_API_KEY, callId),
  ]);

  // CC grounding — mode 'on' for the Sandy pipeline (DispositionDesign §5)
  const ccCtx = await fetchCallContext(db, teamId, {
    entry_point: details.entry_point_call_id ?? "",
    call_id: callId,
    master: details.master_call_id ?? "",
  });
  const callContextText = buildCallContextBlock(ccCtx);

  // SOP retrieval (PulpoConnection §4.2, mode=on — v1-essential): the
  // verified disposition is the query; empty/sub-τ retrieval falls through
  // to the sop_context_missing conservative path automatically.
  const sop = await fetchSopContext({
    pulpoUrl: env.PULPO_MCP_URL,
    pulpoToken: env.PULPO_MCP_TOKEN,
    dispositionCategory: ccCtx?.disposition_category ?? null,
    disposition: ccCtx?.disposition ?? null,
    transcriptText: transcript.transcript_text,
  });

  const durationMs = Number(details.total_duration ?? 0);
  const extraNotes = buildLongCallNote(config.prompt_config, durationMs);
  const teamLabel = teamId === "sales" ? "Sales" : "Member Support";
  const teamLine = teamId === "sales" ? "sales line" : "member support line";
  const promptExtras = {
    sopTitle: sop.sop_title,
    sopContent: sop.block_text,
    agentName: agentName,
    extraNotes,
    callContextText,
    teamContext:
      `TEAM CONTEXT: this call was answered by the ${teamLabel} team on the ` +
      `${teamLine}. Evaluate against ${teamLabel} expectations; if any SOP ` +
      `below belongs to a different team or product line, do not hold the ` +
      `agent to it.`,
  };

  const payload = {
    call_id: callId,
    team_id: teamId,
    pipeline: "two_stage",
    annotator: {
      model: "gemini-2.5-flash",
      system: ANNOTATOR_SYSTEM,
      prompt: buildAnnotatorPrompt(transcript.transcript_text, transcript.moments_display),
      response_schema: ANNOTATOR_RESPONSE_SCHEMA,
      thinking_budget: 4096,
      max_output_tokens: 65536,
    },
    judge: {
      provider: "anthropic",
      model: "claude-sonnet-5", // pinned — matches Railway's SCORING_ANTHROPIC_MODEL
      system: buildJudgeSystemPrompt(config.prompt_config),
      prompt_template: buildJudgePromptTemplate(config.prompt_config, promptExtras),
      max_tokens: 16384,
    },
    single_stage: {
      model: "gemini-2.5-flash",
      system: buildSystemPrompt(config.prompt_config),
      prompt: buildScoringPrompt(config.prompt_config, transcript.transcript_text, promptExtras),
      temperature: 0.2,
      max_output_tokens: 65536,
    },
    // context the callback needs to persist the evaluation
    persist: {
      agent_id: agentRow.id,
      agent_name: agentName,
      agent_email: agentEmail,
      manager_email: managerEmail,
      caller_name: details.caller_name ?? "",
      caller_phone: details.caller_phone ?? "",
      call_connected_at: epochToIso(details.date_connected),
      call_ended_at: epochToIso(details.date_ended),
      call_duration_ms: durationMs || null,
      dialpad_entry_point_call_id: details.entry_point_call_id || null,
      dialpad_master_call_id: details.master_call_id || null,
      cc_stamps: ccCtx
        ? {
            disposition_category: ccCtx.disposition_category,
            disposition: ccCtx.disposition,
            ai_csat: ccCtx.ai_csat,
            cc_call_id: ccCtx.cc_call_id,
          }
        : null,
      transcript_display: transcript.transcript_display,
      moments_display: transcript.moments_display,
      flagged_long_call: durationMs > 25 * 60 * 1000,
      // PulpoConnection §4.2 step 6 — provenance stamped in shadow AND on
      sop_used: sop.sop_title || null,
      pulpo_docs: sop.provenance,
      sop_skipped_reason: sop.skipped_reason || null,
      rescore_of: opts.rescoreOf ?? null,
    },
  };

  // trigger via the Sandy platform (template src/workflow.ts wiring)
  const { listTriggerableWorkflows, triggerWorkflowWithCallback } = await import(
    "../workflow.js"
  );
  const known = await listTriggerableWorkflows();
  const wfId =
    known.find((w: any) => w.name === WORKFLOW_NAME)?.id ??
    "25dec973-c4ab-4122-b2ce-9d3a03c802d8";
  const run = await triggerWorkflowWithCallback(wfId, WORKFLOW_NAME, request, payload);
  // Two rows: the platform run id (the generic callback handler updates it)
  // and the job id (idempotency + the status poll the console uses).
  const stamp = JSON.stringify({ sandy_run_id: run?.id ?? null, call_id: callId, job_id: jobId });
  for (const key of [run?.id, jobId]) {
    if (!key) continue;
    await db
      .prepare(
        "INSERT INTO workflow_runs (run_id, workflow_name, status, result, created_at) VALUES (?, ?, 'running', ?, strftime('%Y-%m-%dT%H:%M:%fZ','now')) " +
          "ON CONFLICT(run_id) DO UPDATE SET status='running', result=excluded.result"
      )
      .bind(key, WORKFLOW_NAME, stamp)
      .run();
  }
  return json({ job_id: jobId, status: "pending", sandy_run_id: run?.id ?? null });
}

export async function scoreStatus(
  db: D1Database,
  jobId: string
): Promise<Response> {
  const row = await db
    .prepare("SELECT run_id, status, result, created_at FROM workflow_runs WHERE run_id = ?")
    .bind(jobId)
    .first<any>();
  if (!row) return json({ detail: `no job ${jobId}` }, 404);
  let result: any = {};
  try {
    result = row.result ? JSON.parse(row.result) : {};
  } catch {}
  // Flattened so the lookup poller reads status/state/scoring_status/
  // overall_score/error at the top level, like Railway's job dict.
  return json({
    ...result,
    job_id: row.run_id,
    status: row.status === "running" ? "pending" : row.status,
    error: result.ok === false ? result.note : result.error ?? null,
    created_at: row.created_at,
  });
}

// ── lifecycle actions (ScorecardActionsDesign §4.2 rescore / §4.4 delete) ──
// Shadow-period doctrine: Sandy actions touch SANDY-BORN evaluations only
// (id >= SANDY_ID_BASE). A Railway-born row edited here would be silently
// reverted by the next sync — refuse loudly instead; act on Railway.

const SANDY_BASE = 10_000_000;

async function resolveEval(db: D1Database, teamId: string, ref: string) {
  return db
    .prepare(
      `SELECT id, agent_id, agent_name_raw, agent_email, overall_score,
              models_used, dialpad_call_id, dialpad_entry_point_call_id, state
       FROM qa_evaluations
       WHERE team_id = ? AND (dialpad_call_id = ? OR dialpad_entry_point_call_id = ?)
       ORDER BY id DESC LIMIT 1`
    )
    .bind(teamId, ref, ref)
    .first<any>();
}

async function auditRow(
  db: D1Database,
  fields: { role: string; evaluator: string; agentEmail: string; agentName: string;
    callId: string; team: string; action: string; notes: string }
) {
  const idRow = await db
    .prepare("SELECT COALESCE(MAX(id) + 1, ?) AS v FROM qa_score_audit WHERE id >= ?")
    .bind(SANDY_BASE, SANDY_BASE)
    .first<any>();
  await db
    .prepare(
      `INSERT INTO qa_score_audit (id, api_key_role, evaluator_email, agent_email,
        agent_name, call_id, target_team, action, notes) VALUES (?,?,?,?,?,?,?,?,?)`
    )
    .bind(idRow?.v ?? SANDY_BASE, fields.role, fields.evaluator, fields.agentEmail,
      fields.agentName, fields.callId, fields.team, fields.action, fields.notes)
    .run();
}

export async function rescoreEvaluation(
  request: Request,
  db: D1Database,
  teamId: string,
  ref: string,
  env: { DIALPAD_API_KEY?: string; PULPO_MCP_URL?: string; PULPO_MCP_TOKEN?: string }
): Promise<Response> {
  let body: any = {};
  try {
    body = await request.json();
  } catch {}
  const evaluatorEmail = (body.evaluator_email ?? "").trim().toLowerCase();
  if (!evaluatorEmail) return json({ detail: "evaluator_email required" }, 422);
  const ev = await resolveEval(db, teamId, ref);
  if (!ev) return json({ detail: "No evaluation matches this call id" }, 404);
  if (ev.id < SANDY_BASE)
    return json(
      { detail: "This evaluation is Railway-owned during the shadow period — re-score it on Railway." },
      409
    );
  let supersededModel: string | null = null;
  try {
    supersededModel = JSON.parse(ev.models_used ?? "{}")?.text?.model ?? null;
  } catch {}
  const callId = ev.dialpad_call_id;
  await auditRow(db, {
    role: "privileged", evaluator: evaluatorEmail, agentEmail: ev.agent_email ?? "",
    agentName: ev.agent_name_raw, callId, team: teamId, action: "rescored",
    notes: `superseded_score=${ev.overall_score ?? "null"} superseded_model=${supersededModel ?? "?"}`,
  });
  const trigger = await scoreTriggerInternal(request, db, teamId, env, {
    callId,
    agentEmail: ev.agent_email ?? "",
    managerEmail: evaluatorEmail,
    rescoreOf: ev.id,
  });
  if (trigger.status !== 200) return trigger;
  const data: any = await trigger.clone().json();
  return json({ ...data, superseded_score: ev.overall_score, superseded_model: supersededModel });
}

export async function deleteEvaluation(
  request: Request,
  db: D1Database,
  teamId: string,
  ref: string,
  url: URL
): Promise<Response> {
  const evaluatorEmail =
    (url.searchParams.get("evaluator_email") ?? "").trim().toLowerCase() || "unknown";
  const ev = await resolveEval(db, teamId, ref);
  if (!ev) return json({ detail: "No evaluation matches this call id" }, 404);
  if (ev.id < SANDY_BASE)
    return json(
      { detail: "This evaluation is Railway-owned during the shadow period — delete it on Railway." },
      409
    );
  await db.prepare("DELETE FROM qa_evaluation_sections WHERE evaluation_id = ?").bind(ev.id).run();
  await db.prepare("DELETE FROM qa_evaluations WHERE id = ?").bind(ev.id).run();
  // CC orphan latch (§3.9/§4.2): scored stays monotonic, orphaned flips on
  const now = new Date().toISOString();
  await db
    .prepare(
      `UPDATE cc_calls SET evaluation_orphaned = 1, evaluation_orphaned_at = ?
       WHERE team_id = ? AND (dialpad_call_id = ? OR dialpad_entry_point_call_id = ?)`
    )
    .bind(now, teamId, ev.dialpad_call_id ?? "", ev.dialpad_entry_point_call_id ?? "")
    .run();
  await auditRow(db, {
    role: "privileged", evaluator: evaluatorEmail, agentEmail: ev.agent_email ?? "",
    agentName: ev.agent_name_raw, callId: ev.dialpad_call_id ?? ref, team: teamId,
    action: "evaluation_orphaned", notes: `deleted eval ${ev.id} (score=${ev.overall_score ?? "null"})`,
  });
  return json({ ok: true, deleted_evaluation_id: ev.id });
}

// ── scorecard payload + approve / override / edit (§4.1 / §4.3 / S7) ───────

async function evalSections(db: D1Database, evalId: number) {
  return (
    await db
      .prepare(
        "SELECT section_id, section_number, score_type, numeric_score, binary_value, score_source, confidence, reasoning FROM qa_evaluation_sections WHERE evaluation_id = ? ORDER BY section_number"
      )
      .bind(evalId)
      .all<any>()
  ).results;
}

// Restored-job dict — scoring.py::_job_from_ref shape, rendered forever
// for any evaluation (the S1 reconstruction seam).
export async function scorecardPayload(
  db: D1Database,
  teamId: string,
  ref: string
): Promise<Response> {
  const { loadTeamConfig: load } = await import("../lib/teamConfig.js");
  const config = await load(db, teamId);
  const ev = await db
    .prepare(
      `SELECT * FROM qa_evaluations
       WHERE team_id = ? AND (dialpad_call_id = ? OR dialpad_entry_point_call_id = ?)
       ORDER BY id DESC LIMIT 1`
    )
    .bind(teamId, ref, ref)
    .first<any>();
  if (!ev) {
    // fall back to the live job store (in-flight scoring)
    const job = await db
      .prepare("SELECT run_id, status, result FROM workflow_runs WHERE run_id = ?")
      .bind(ref)
      .first<any>();
    if (!job) return json({ detail: "Job not found" }, 404);
    let r: any = {};
    try { r = job.result ? JSON.parse(job.result) : {}; } catch {}
    return json({
      ...r,
      status: job.status === "running" ? "pending" : job.status,
      error: r.ok === false ? r.note : r.error ?? null,
    });
  }
  const rows = await evalSections(db, ev.id);
  const byId = new Map(rows.map((r: any) => [r.section_id, r]));
  const sections = config.prompt_config.sections
    .filter((s: any) => !s.auto_value)
    .map((s: any) => {
      const row: any = byId.get(s.id) ?? {};
      const isYn = String(s.score_type).endsWith("yn");
      return {
        id: s.id,
        name: s.name,
        score: row.numeric_score ?? null,
        score_type: isYn ? "yn" : "numeric",
        yn_value: row.binary_value ?? null,
        confidence: row.confidence ?? null,
        reasoning: row.reasoning ?? null,
        audio_dependent: !!s.audio_dependent,
        flags: [],
      };
    });
  let model: string | null = null;
  try { model = JSON.parse(ev.models_used ?? "{}")?.text?.model ?? null; } catch {}
  return json({
    status: "complete",
    state: ev.state,
    scoring_status: ev.scoring_status,
    evaluation_id: ev.id,
    call_id: ev.dialpad_call_id ?? ev.dialpad_entry_point_call_id ?? ref,
    agent_name: ev.agent_name_raw,
    agent_email: ev.agent_email ?? "",
    overall_score: ev.overall_score,
    restored_from_db: true,
    sandy_born: ev.id >= SANDY_BASE,
    scorecard: {
      sections,
      manager_email: ev.evaluator_email ?? "",
      dialpad_link: ev.dialpad_link,
      call_summary: ev.call_summary ?? "",
      key_strengths: ev.key_strengths ?? "",
      opportunities: ev.opportunities ?? "",
      agent_name: ev.agent_name_raw,
      model,
    },
  });
}

async function coachingReceipt(
  db: D1Database,
  ev: any,
  teamId: string,
  evaluatorEmail: string,
  role: string,
  plan: string
): Promise<number> {
  const idRow = await db
    .prepare("SELECT COALESCE(MAX(id) + 1, ?) AS v FROM qa_coachings WHERE id >= ?")
    .bind(SANDY_BASE, SANDY_BASE)
    .first<any>();
  const cid = idRow?.v ?? SANDY_BASE;
  await db
    .prepare(
      `INSERT INTO qa_coachings (id, agent_id, team_id, conducted_by_role,
        conducted_by_email, status, action_plan) VALUES (?,?,?,?,?, 'pending', ?)`
    )
    .bind(cid, ev.agent_id, teamId, role, evaluatorEmail, plan)
    .run();
  await db
    .prepare(
      `INSERT INTO qa_coaching_evaluations (id, coaching_id, evaluation_id, opportunities_snapshot)
       VALUES (?,?,?,?)`
    )
    .bind(cid, cid, ev.id, ev.opportunities ?? null)
    .run();
  return cid;
}

// Shared finalize-with-edits core: approve (§4.1), edit-of-finalized (S7).
export async function approveEvaluation(
  request: Request,
  db: D1Database,
  teamId: string,
  ref: string,
  opts: { editOfFinalized: boolean }
): Promise<Response> {
  let body: any = {};
  try { body = await request.json(); } catch { return json({ detail: "JSON body required" }, 422); }
  const evaluatorEmail = (body.evaluator_email ?? "").trim().toLowerCase();
  if (!evaluatorEmail) return json({ detail: "evaluator_email required" }, 422);

  const ev = await db
    .prepare(
      `SELECT * FROM qa_evaluations
       WHERE team_id = ? AND (dialpad_call_id = ? OR dialpad_entry_point_call_id = ?)
       ORDER BY id DESC LIMIT 1`
    )
    .bind(teamId, ref, ref)
    .first<any>();
  if (!ev) return json({ detail: "No evaluation matches this call id" }, 404);
  if (ev.id < SANDY_BASE)
    return json(
      { detail: "This evaluation is Railway-owned during the shadow period — act on Railway." },
      409
    );
  const wasFlagged = ev.scoring_status === "flagged_human_review";
  if ((wasFlagged || opts.editOfFinalized) && body.acknowledged !== true)
    return json(
      { detail: "acknowledged=true required — you are modifying an agent's progression record" },
      422
    );

  const { loadTeamConfig: load } = await import("../lib/teamConfig.js");
  const config = await load(db, teamId);
  const existing = await evalSections(db, ev.id);
  const existingById = new Map(existing.map((r: any) => [r.section_id, r]));

  // sections payload → rows; detect edits (source flip to ai_reviewed)
  const rows: any[] = [];
  let edited = false;
  for (const sec of config.prompt_config.sections) {
    if (sec.auto_value) {
      const prev: any = existingById.get(sec.id);
      rows.push({
        section_id: sec.id, section_number: sec.section_number,
        score_type: "auto_value",
        numeric_score: null,
        binary_value: prev?.binary_value ?? (sec.auto_value.toUpperCase().startsWith("Y") ? "Y" : "N"),
        score_source: "auto_value", ai_provider: null,
        confidence: null, reasoning: null,
      });
      continue;
    }
    const sent = (body.sections ?? []).find((s: any) => s.id === sec.id);
    if (!sent) return json({ detail: `sections payload missing '${sec.id}'` }, 422);
    const isYn = String(sec.score_type).endsWith("yn") || sent.score_type === "yn";
    const numeric =
      sent.score !== null && sent.score !== undefined ? Math.trunc(Number(sent.score)) : null;
    const yn = sent.yn_value ?? null;
    if (numeric === null && yn === null)
      return json(
        { detail: `section '${sec.id}': needs a score or yn_value before approval` },
        422
      );
    const prev: any = existingById.get(sec.id);
    const changed =
      !prev || (prev.numeric_score ?? null) !== numeric || (prev.binary_value ?? null) !== yn;
    if (changed) edited = true;
    const dbScoreType =
      sec.score_type === "numeric" ? "numeric"
      : sec.score_type === "yn" ? "binary"
      : sec.score_type === "manual" ? "manual_numeric"
      : "manual_binary";
    rows.push({
      section_id: sec.id, section_number: sec.section_number,
      score_type: dbScoreType,
      numeric_score: dbScoreType.includes("numeric") && yn !== "NA" ? numeric : null,
      binary_value: dbScoreType.includes("numeric") && yn !== "NA" ? null : yn,
      score_source: changed ? "manual" : prev?.score_source ?? "ai",
      ai_provider: changed ? null : prev?.score_source === "ai" ? "gemini" : null,
      confidence: sent.confidence ?? prev?.confidence ?? null,
      reasoning: sent.reasoning ?? prev?.reasoning ?? null,
    });
  }

  const answers: Record<string, number | string> = {};
  for (const r of rows) {
    if (r.binary_value !== null) answers[r.section_id] = r.binary_value;
    else if (r.numeric_score !== null) answers[r.section_id] = r.numeric_score;
  }
  const fvRow = await db
    .prepare(
      "SELECT formula_version, formula_json FROM qa_formula_versions WHERE formula_version = ?"
    )
    .bind(ev.formula_version)
    .first<any>();
  if (!fvRow) return json({ detail: `formula ${ev.formula_version} not archived` }, 500);
  const { evaluateFormula: evalF, quantizeScore: quant } = await import("../lib/ruleEngine.js");
  let overall: number;
  try {
    overall = quant(evalF(JSON.parse(fvRow.formula_json), answers).final_score);
  } catch (err) {
    return json(
      { detail: `formula rejected the edited sections: ${String((err as any)?.message ?? err).slice(0, 300)}` },
      422
    );
  }

  const now = new Date().toISOString();
  await db.prepare("DELETE FROM qa_evaluation_sections WHERE evaluation_id = ?").bind(ev.id).run();
  for (const r of rows) {
    await db
      .prepare(
        `INSERT INTO qa_evaluation_sections (id, evaluation_id, section_id, section_number,
          score_type, numeric_score, binary_value, score_source, ai_provider,
          confidence, reasoning) VALUES (?,?,?,?,?,?,?,?,?,?,?)`
      )
      .bind(
        ev.id * 100 + r.section_number, ev.id, r.section_id, r.section_number,
        r.score_type, r.numeric_score, r.binary_value, r.score_source,
        r.ai_provider, r.confidence, r.reasoning
      )
      .run();
  }
  await db
    .prepare(
      `UPDATE qa_evaluations SET state='finalized', scoring_status='complete',
        source=?, evaluator_email=?, overall_score=?,
        key_strengths=?, opportunities=?,
        approved_at=COALESCE(approved_at, ?), finalized_at=?
       WHERE id=?`
    )
    .bind(
      edited ? "ai_reviewed" : ev.source, evaluatorEmail, overall,
      body.key_strengths ?? ev.key_strengths, body.opportunities ?? ev.opportunities,
      now, now, ev.id
    )
    .run();

  // S7: an edit-of-finalized creates the coaching receipt binding the
  // notification duty (email dispatch itself is a later GAS-integration slice).
  let coachingId: number | null = null;
  if (opts.editOfFinalized && edited) {
    coachingId = await coachingReceipt(
      db, ev, teamId, evaluatorEmail, "manager",
      `Scorecard edited post-finalize (was ${ev.overall_score}, now ${overall}). Notify the agent.`
    );
  }
  await auditRow(db, {
    role: "privileged", evaluator: evaluatorEmail, agentEmail: ev.agent_email ?? "",
    agentName: ev.agent_name_raw, callId: ev.dialpad_call_id ?? ref, team: teamId,
    action: "approved",
    notes: `${wasFlagged ? "resolved_flagged " : ""}${edited ? "edited " : ""}score=${overall}${coachingId ? ` coaching=${coachingId}` : ""}`,
  });
  await db
    .prepare("INSERT INTO qa_events (team_id, type, payload) VALUES (?, 'eval_approved', ?)")
    .bind(
      teamId,
      JSON.stringify({
        call_id: ev.dialpad_call_id ?? "", eval_id: ev.dialpad_entry_point_call_id ?? ev.dialpad_call_id ?? "",
        history_row: null, agent: ev.agent_name_raw, evaluator_email: evaluatorEmail,
        overall_score: overall, summary: (ev.call_summary ?? "").slice(0, 280),
        strengths: (body.key_strengths ?? ev.key_strengths ?? "").slice(0, 280),
        opportunities: (body.opportunities ?? ev.opportunities ?? "").slice(0, 280),
        dialpad_link: ev.dialpad_link ?? "", timestamp: now,
      })
    )
    .run();
  return json({
    ok: true, evaluation_id: ev.id, overall_score: overall, state: "finalized",
    edited, coaching_id: coachingId, email: "not_ported_yet",
  });
}

export async function overrideEvaluation(
  request: Request,
  db: D1Database,
  teamId: string,
  ref: string
): Promise<Response> {
  let body: any = {};
  try { body = await request.json(); } catch { return json({ detail: "JSON body required" }, 422); }
  const evaluatorEmail = (body.evaluator_email ?? "").trim().toLowerCase();
  if (!evaluatorEmail) return json({ detail: "evaluator_email required" }, 422);
  if (body.acknowledged !== true)
    return json({ detail: "acknowledged=true required for an override" }, 422);
  const score = Number(body.overall_score);
  if (!Number.isFinite(score)) return json({ detail: "overall_score required" }, 422);
  const ev = await resolveEval(db, teamId, ref);
  if (!ev) return json({ detail: "No evaluation matches this call id" }, 404);
  if (ev.id < SANDY_BASE)
    return json(
      { detail: "This evaluation is Railway-owned during the shadow period — act on Railway." },
      409
    );
  const now = new Date().toISOString();
  await db
    .prepare(
      `UPDATE qa_evaluations SET overall_score=?, source='ai_reviewed',
        evaluator_email=?, state='finalized', scoring_status='complete',
        approved_at=COALESCE(approved_at, ?), finalized_at=? WHERE id=?`
    )
    .bind(score, evaluatorEmail, now, now, ev.id)
    .run();
  // §4.3: the override ALWAYS creates the coaching receipt + duty.
  const coachingId = await coachingReceipt(
    db, ev, teamId, evaluatorEmail, body.conducted_by_role ?? "manager",
    `Score overridden to ${score} (was ${ev.overall_score}). ${body.reasoning ?? ""}` +
      (body.sop_gap?.note ? ` SOP gap: ${body.sop_gap.note}` : "")
  );
  await auditRow(db, {
    role: "privileged", evaluator: evaluatorEmail, agentEmail: ev.agent_email ?? "",
    agentName: ev.agent_name_raw, callId: ev.dialpad_call_id ?? ref, team: teamId,
    action: "overridden",
    notes: `override=${score} was=${ev.overall_score ?? "null"} coaching=${coachingId}${body.sop_gap?.note ? " sop_gap" : ""}`,
  });
  return json({ ok: true, evaluation_id: ev.id, overall_score: score, coaching_id: coachingId });
}

// ── callback persist ───────────────────────────────────────────────────────

function sectionRowsFromScorecard(config: TeamConfig, scorecard: any) {
  // AI-scored sections from the model output; manual/auto sections filled
  // with their designed defaults (manual_yn/manual → NA-state; auto_value →
  // its hardcoded Y). Shapes mirror eval_store's Stage-1 write.
  const bySecId = new Map<string, any>(
    (scorecard.sections ?? []).map((s: any) => [s.id, s])
  );
  const rows: any[] = [];
  for (const sec of config.prompt_config.sections) {
    const dbScoreType =
      sec.score_type === "numeric"
        ? "numeric"
        : sec.score_type === "yn"
          ? "binary"
          : sec.score_type === "manual"
            ? "manual_numeric"
            : sec.score_type === "manual_yn"
              ? "manual_binary"
              : "binary";
    if (sec.auto_value) {
      rows.push({
        section_id: sec.id, section_number: sec.section_number,
        score_type: "auto_value", numeric_score: null,
        binary_value: sec.auto_value.toUpperCase().startsWith("Y") ? "Y" : "N",
        score_source: "auto_value", ai_provider: null, model: null,
        confidence: null, reasoning: null,
      });
      continue;
    }
    if (sec.score_type === "manual" || sec.score_type === "manual_yn") {
      rows.push({
        section_id: sec.id, section_number: sec.section_number,
        score_type: dbScoreType,
        numeric_score: null, binary_value: "NA",
        score_source: "manual_default", ai_provider: null, model: null,
        confidence: null, reasoning: null,
      });
      continue;
    }
    const out = bySecId.get(sec.id);
    if (!out) throw new Error(`scorecard missing section '${sec.id}'`);
    const ynValue = out.yn_value && out.yn_value !== "null" ? out.yn_value : null;
    const numeric =
      out.score !== null && out.score !== undefined && out.score !== "null"
        ? Math.trunc(Number(out.score))
        : null;
    if (numeric === null && ynValue === null)
      throw new Error(`section '${sec.id}': neither score nor yn_value`);
    rows.push({
      section_id: sec.id, section_number: sec.section_number,
      score_type: dbScoreType,
      numeric_score: dbScoreType === "numeric" && ynValue === "NA" ? null : numeric,
      binary_value: dbScoreType === "numeric" && ynValue !== "NA" ? null : ynValue,
      score_source: "ai",
      // D1/PG CHECK admits gemini|landgpt only; true judge provenance rides
      // models_used on the evaluation row (matches Railway's write).
      ai_provider: "gemini",
      model: null,
      confidence: out.confidence ?? null,
      reasoning: out.reasoning ?? null,
    });
  }
  return rows;
}

function answersFromSectionRows(rows: any[]): Record<string, number | string> {
  const answers: Record<string, number | string> = {};
  for (const r of rows) {
    if (r.binary_value !== null) answers[r.section_id] = r.binary_value;
    else if (r.numeric_score !== null) answers[r.section_id] = r.numeric_score;
  }
  return answers;
}

export async function scoringCallback(
  body: any,
  db: D1Database
): Promise<{ ok: boolean; note: string }> {
  const jobStatus = body.status === "complete" ? "complete" : "error";
  const p = body; // workflow result payload
  const teamId = p.team_id;
  const persist = p.persist ?? null;

  if (jobStatus === "error" || !p.scorecard_raw || !persist) {
    return { ok: false, note: p.error ?? "no scorecard in callback" };
  }
  const config = await loadTeamConfig(db, teamId);
  const scorecard = p.scorecard_raw;

  const sectionRows = sectionRowsFromScorecard(config, scorecard);
  const answers = answersFromSectionRows(sectionRows);

  // active version stamps (score_compute.get_active_versions port)
  const fvRow = await db
    .prepare(
      "SELECT formula_version, formula_json FROM qa_formula_versions WHERE team_id = ? AND effective_until IS NULL ORDER BY effective_from DESC LIMIT 1"
    )
    .bind(teamId)
    .first<any>();
  if (!fvRow) return { ok: false, note: `no active formula for ${teamId}` };
  const formula = JSON.parse(fvRow.formula_json);

  const result = evaluateFormula(formula, answers);
  const overallScore = quantizeScore(result.final_score);

  // §3.14 human-review gate
  let flagged = false;
  for (const trig of formula.human_review_triggers ?? []) {
    const a = answers[trig.section_id];
    if (typeof a === "number" && a <= trig.max_score_to_trigger) {
      flagged = true;
      break;
    }
  }

  // Sandy-born rows live in a reserved HIGH id range so the shadow sync
  // (which wipes + re-imports the Railway-owned range) never touches
  // them, and future Railway ids can never collide. A rescore REPLACES
  // on the same row id (§4.2).
  let newEvalId: number;
  if (persist.rescore_of) {
    newEvalId = persist.rescore_of;
    await db
      .prepare("DELETE FROM qa_evaluation_sections WHERE evaluation_id = ?")
      .bind(newEvalId)
      .run();
    await db.prepare("DELETE FROM qa_evaluations WHERE id = ?").bind(newEvalId).run();
  } else {
    const idRow = await db
      .prepare(
        "SELECT COALESCE(MAX(id) + 1, ?) AS next_id FROM qa_evaluations WHERE id >= ?"
      )
      .bind(SANDY_BASE, SANDY_BASE)
      .first<{ next_id: number }>();
    newEvalId = idRow?.next_id ?? SANDY_BASE;
  }

  const now = new Date().toISOString();
  const modelsUsed = {
    audio: p.annotator_model ? { provider: "gemini", model: p.annotator_model } : undefined,
    text: { provider: p.scorer_provider, model: p.scorer_model },
    ...(p.pipeline_fallback_reason ? { fallback: p.pipeline_fallback_reason } : {}),
  };
  const meta = {
    moments: persist.moments_display ?? [],
    transcript_display: persist.transcript_display ?? [],
    sop_used: persist.sop_used ?? null,
    pulpo_docs: persist.pulpo_docs ?? [],
    ...(persist.sop_skipped_reason ? { sop_skipped_reason: persist.sop_skipped_reason } : {}),
    sandy_pipeline: {
      run_id: body.run_id,
      timings_ms: p.timings_ms,
      annotate_diag: p.annotate_diag,
    },
  };

  const evalInsert = await db
    .prepare(
      `INSERT INTO qa_evaluations (
        id, team_id, agent_id, agent_name_raw, agent_email, evaluator_email,
        state, source, call_connected_at, call_ended_at, call_duration_ms,
        language, dialpad_call_id, dialpad_entry_point_call_id,
        dialpad_master_call_id, dialpad_link, caller_name, caller_phone,
        call_summary, annotated_transcript, key_strengths, opportunities,
        overall_score, formula_version, rubric_version, models_used,
        ai_provider_primary, sampling_status, scoring_status,
        created_at, approved_at, finalized_at,
        dialpad_disposition_category, dialpad_disposition, ai_csat,
        command_center_call_id, dialpad_call_metadata
      ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`
    )
    .bind(
      newEvalId,
      teamId, persist.agent_id, persist.agent_name, persist.agent_email,
      flagged ? null : persist.manager_email,
      flagged ? "draft" : "finalized", "ai",
      persist.call_connected_at, persist.call_ended_at, persist.call_duration_ms,
      p.annotation?.language_detected ?? null,
      p.call_id, persist.dialpad_entry_point_call_id, persist.dialpad_master_call_id,
      `https://dialpad.com/callhistory/callreview/${persist.dialpad_entry_point_call_id || p.call_id}`,
      persist.caller_name || null, persist.caller_phone || null,
      scorecard.call_summary ?? null,
      p.annotation ? JSON.stringify(p.annotation) : null,
      scorecard.key_strengths ?? null, scorecard.opportunities ?? null,
      flagged ? null : overallScore,
      fvRow.formula_version, config.rubric_version,
      JSON.stringify(modelsUsed), "gemini",
      "not_sampled",
      flagged ? "flagged_human_review" : "complete",
      now, flagged ? null : now, flagged ? null : now,
      persist.cc_stamps?.disposition_category ?? null,
      persist.cc_stamps?.disposition ?? null,
      persist.cc_stamps?.ai_csat ?? null,
      persist.cc_stamps?.cc_call_id ?? null,
      JSON.stringify(meta)
    )
    .run();
  const evalId = newEvalId;

  for (const r of sectionRows) {
    await db
      .prepare(
        `INSERT INTO qa_evaluation_sections (
          id, evaluation_id, section_id, section_number, score_type,
          numeric_score, binary_value, score_source, ai_provider, model,
          confidence, reasoning
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)`
      )
      .bind(
        // deterministic high-range section ids (evalId × 100 + number)
        evalId * 100 + r.section_number,
        evalId, r.section_id, r.section_number, r.score_type,
        r.numeric_score, r.binary_value, r.score_source, r.ai_provider,
        r.model, r.confidence, r.reasoning
      )
      .run();
  }

  if (!flagged) {
    await db
      .prepare("INSERT INTO qa_events (team_id, type, payload) VALUES (?, 'eval_approved', ?)")
      .bind(
        teamId,
        JSON.stringify({
          call_id: p.call_id,
          eval_id: persist.dialpad_entry_point_call_id || p.call_id,
          history_row: null,
          agent: persist.agent_name,
          evaluator_email: persist.manager_email,
          overall_score: overallScore,
          summary: (scorecard.call_summary ?? "").slice(0, 280),
          strengths: (scorecard.key_strengths ?? "").slice(0, 280),
          opportunities: (scorecard.opportunities ?? "").slice(0, 280),
          dialpad_link: `https://dialpad.com/callhistory/callreview/${persist.dialpad_entry_point_call_id || p.call_id}`,
          timestamp: now,
        })
      )
      .run();
  }

  return {
    ok: true,
    note: flagged
      ? `evaluation ${evalId} parked for human review`
      : `evaluation ${evalId} finalized at ${overallScore}`,
    evaluation_id: evalId,
    state: flagged ? "draft" : "finalized",
    scoring_status: flagged ? "flagged_human_review" : "complete",
    overall_score: flagged ? null : overallScore,
    eval_id: persist.dialpad_entry_point_call_id || p.call_id,
  };
}

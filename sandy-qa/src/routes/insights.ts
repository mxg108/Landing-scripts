// Insights routes + persist — CoachingLoopSpec §8 layer 2 (app side).
// Facts are computed by coachingFacts.ts; prompts are built HERE; the
// qa-insights workflow runs the Claude call; the callback lands back in
// this module and persists into qa_assessments/qa_assessment_sections —
// the Railway-parity shape the one-pager and dashboard already read —
// at Sandy-born high-range ids.

import { loadTeamConfig, type TeamConfig } from "../lib/teamConfig.js";
import { agentFacts, teamFacts, type AgentFactsWindow } from "../lib/coachingFacts.js";
import { canCoach, resolveAccess } from "../lib/rbac.js";

const WORKFLOW_NAME = "qa-insights";
const SANDY_BASE = 10_000_000;
const INSIGHTS_MODEL = "claude-sonnet-5"; // §11.5 — judge parity, pinned
const FRESH_MS = 60 * 60 * 1000; // dashboard cache contract (1h, Railway parity)

const json = (data: unknown, status = 200) =>
  new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json" },
  });

// ── prompt build ───────────────────────────────────────────────────────────

function assessedSections(config: TeamConfig) {
  return config.sections_by_number.filter(
    (s) => !s.auto_value && !["manual", "manual_yn"].includes(s.score_type)
  );
}

export function buildProgressionPrompts(config: TeamConfig, facts: any) {
  const sections = assessedSections(config);
  const system =
    "You are a QA coaching analyst for Landing's call teams. You write " +
    "grounded, specific progression assessments for human supervisors and " +
    "trainers. Every claim must trace to the fact sheet you are given — " +
    "never invent numbers, and cite the figures you use. When coaching " +
    "sessions appear in the facts, weigh them explicitly: commitments met " +
    "or not met, and whether post-coaching numbers moved. Be direct and " +
    "useful; no filler.";
  const prompt =
    `FACT SHEET (deterministic, computed from the QA database):\n` +
    `${JSON.stringify(facts, null, 2)}\n\n` +
    `SECTIONS TO ASSESS (use these exact ids):\n` +
    sections.map((s) => `- ${s.id}: ${s.name}`).join("\n") +
    `\n\nRespond with ONLY a JSON object (no markdown fences, no prose ` +
    `outside it) in exactly this shape:\n` +
    `{"overall_assessment": "<3-6 sentences: trajectory, coaching response ` +
    `if any coachings are present (commitments met/not met and whether the ` +
    `numbers moved), and the single most important focus now>",\n` +
    ` "sections": [{"section_id": "<id from the list>", "trend": ` +
    `"improving"|"stable"|"declining", "summary": "<1-2 sentences with ` +
    `figures>", "coaching_tip": "<one concrete, actionable tip>"}]}\n` +
    `Include EVERY listed section exactly once.`;
  return { system, prompt };
}

// Fence-tolerant JSON extraction (models occasionally wrap despite
// instructions; anything else is a validation failure, not a guess).
function parseItemJson(text: string): any | null {
  const t = text.trim().replace(/^```(?:json)?\s*/i, "").replace(/\s*```$/, "");
  try {
    return JSON.parse(t);
  } catch {
    return null;
  }
}

// ── trigger + poll (the dashboard AI card contract) ────────────────────────

const agentSlug = (name: string) => name.toLowerCase().replace(/[^a-z0-9]+/g, "-");

async function rosterAgent(db: D1Database, teamId: string, agentName: string) {
  const a = agentName.trim().toLowerCase();
  return db
    .prepare(
      `SELECT id, name, canonical_name FROM qa_agents
       WHERE team_id = ? AND active = 1
         AND (LOWER(name) = ? OR LOWER(canonical_name) = ?) LIMIT 1`
    )
    .bind(teamId, a, a)
    .first<any>();
}

async function currentAssessment(
  db: D1Database,
  teamId: string,
  agentId: number,
  days: number
) {
  const row = await db
    .prepare(
      `SELECT * FROM qa_assessments
       WHERE team_id = ? AND agent_id = ? AND time_range_days = ? AND is_current = 1
       ORDER BY generated_at DESC LIMIT 1`
    )
    .bind(teamId, agentId, days)
    .first<any>();
  if (!row) return null;
  const secs = (
    await db
      .prepare(
        "SELECT section_id, trend, summary, coaching_tip FROM qa_assessment_sections WHERE assessment_id = ? ORDER BY section_number"
      )
      .bind(row.id)
      .all<any>()
  ).results;
  return { row, secs };
}

// Railway progression contract: section_assessments keyed by HISTORY id
// (the dashboard renders labels from /team/sections history ids).
function contractShape(config: TeamConfig, row: any, secs: any[]) {
  const historyOf = new Map(
    config.sections_by_number.map((s) => [s.id, s.history_id || s.id])
  );
  const section_assessments: Record<string, any> = {};
  for (const s of secs) {
    section_assessments[historyOf.get(s.section_id) ?? s.section_id] = {
      trend: s.trend,
      summary: s.summary,
      coaching_tip: s.coaching_tip,
    };
  }
  return {
    ready: true,
    overall_assessment: row.overall_assessment,
    section_assessments,
    evaluations_included: row.evaluations_included,
    generated_at: row.generated_at,
    models_used: row.models_used ? JSON.parse(row.models_used) : null,
  };
}

export async function progressionRequest(
  request: Request,
  db: D1Database,
  teamId: string,
  agentName: string,
  url: URL
): Promise<Response> {
  const config = await loadTeamConfig(db, teamId);
  const agent = await rosterAgent(db, teamId, agentName);
  if (!agent)
    return json({ detail: `'${agentName}' is not on the ${teamId} roster` }, 404);
  const days = Math.min(365, Math.max(7, parseInt(url.searchParams.get("days") ?? "30", 10) || 30));

  // Fresh persisted assessment → serve it (a page view / repeat click must
  // never spend a Claude call — the one-pager doctrine, extended here).
  const cur = await currentAssessment(db, teamId, agent.id, days);
  if (cur && Date.now() - Date.parse(cur.row.generated_at) < FRESH_MS)
    return json(contractShape(config, cur.row, cur.secs));

  const jobId = `insights-${teamId}-${agentSlug(agent.canonical_name || agent.name)}-${days}`;
  const existing = await db
    .prepare("SELECT run_id, status, result FROM workflow_runs WHERE run_id = ?")
    .bind(jobId)
    .first<any>();

  if (request.method === "GET") {
    if (existing && ["queued", "pending", "running"].includes(existing.status))
      return json({ ready: false, job_id: jobId, status: "pending" }, 202);
    if (cur) return json(contractShape(config, cur.row, cur.secs)); // stale-but-served
    if (existing?.status === "error") {
      let note = "generation failed";
      try { note = JSON.parse(existing.result)?.note ?? note; } catch {}
      return json({ ready: false, status: "error", detail: note }, 200);
    }
    return json({ ready: false, status: "none" }, 200);
  }

  // POST — trigger. Idempotent on the in-flight job.
  if (existing && ["queued", "pending", "running"].includes(existing.status))
    return json({ ready: false, job_id: jobId, status: "pending", deduped: true }, 202);

  const now = Date.now();
  const win: AgentFactsWindow = {
    fromMs: now - days * 86_400_000,
    toMs: now,
    label: `last ${days} days`,
  };
  const facts = await agentFacts(db, config, agent, win);
  if (!facts)
    return json({ detail: `No evaluations for '${agentName}' in the last ${days} days.` }, 404);
  const { system, prompt } = buildProgressionPrompts(config, facts);
  const ref = {
    kind: "progression",
    team_id: teamId,
    agent_id: agent.id,
    agent_name: agent.canonical_name || agent.name,
    days,
    range_start_at: new Date(win.fromMs).toISOString(),
    range_end_at: new Date(win.toMs).toISOString(),
    evaluations_included: facts.evaluations.n,
    rubric_version: config.rubric_version,
    job_id: jobId,
  };
  const trigger = await triggerInsights(db, request, {
    mode: "progression",
    model: { model: INSIGHTS_MODEL, max_tokens: 3000 },
    items: [{ ref, system, prompt }],
  });
  if (!trigger.ok)
    return json({ ready: false, status: "busy", detail: trigger.detail }, 409);
  await db
    .prepare(
      "INSERT INTO workflow_runs (run_id, workflow_name, status, result, created_at) VALUES (?, ?, 'running', ?, strftime('%Y-%m-%dT%H:%M:%fZ','now')) " +
        "ON CONFLICT(run_id) DO UPDATE SET status='running', result=excluded.result"
    )
    .bind(jobId, WORKFLOW_NAME, JSON.stringify({ sandy_run_id: trigger.runId }))
    .run();
  return json({ ready: false, job_id: jobId, status: "pending" }, 202);
}

async function triggerInsights(
  db: D1Database,
  request: Request,
  payload: any
): Promise<{ ok: true; runId: string | null } | { ok: false; detail: string }> {
  const { listTriggerableWorkflows, triggerWorkflowWithCallback } = await import(
    "../workflow.js"
  );
  try {
    // Same resolution as the scoring drain: the internal listing first,
    // hardcoded id as the fallback (the listing matches allowed_applications
    // by app NAME — a registration with the app id was invisible to it,
    // which is exactly how the first live button click failed).
    const known = await listTriggerableWorkflows();
    const wfId =
      known.find((w: any) => w.name === WORKFLOW_NAME)?.id ??
      "e497b54d-b09f-4ddc-81ed-833911edc457";
    const run = await triggerWorkflowWithCallback(wfId, WORKFLOW_NAME, request, payload);
    return { ok: true, runId: run?.id ?? null };
  } catch (err) {
    const msg = String((err as any)?.message ?? err).slice(0, 300);
    return {
      ok: false,
      detail: msg.includes("409")
        ? "insights engine busy — another narrative is generating; retry shortly"
        : msg,
    };
  }
}

// ── EOM batch (day-1 maintenance branch calls this) ────────────────────────
// One run, one item per agent with finalized evals in the closed month AND
// no current assessment already intersecting it (idempotent; also the
// double-spend guard while Railway's own monthly export still writes
// assessments during shadow — Sandy fills gaps, takes over at cutover).

export async function eomAssessmentBatch(
  db: D1Database,
  request: Request,
  teamIds: string[],
  month: string // YYYY-MM (closed month)
): Promise<any> {
  const [y, m] = month.split("-").map(Number);
  const fromMs = Date.parse(`${month}-01T00:00:00Z`) - 8 * 3600_000; // LA-ish month open
  const lastDay = new Date(Date.UTC(y, m, 0)).getUTCDate();
  const toMs = Date.parse(`${month}-${String(lastDay).padStart(2, "0")}T23:59:59Z`) + 8 * 3600_000;
  const days = lastDay;
  const items: any[] = [];
  const skipped: string[] = [];
  for (const teamId of teamIds) {
    const config = await loadTeamConfig(db, teamId);
    const agents = (
      await db
        .prepare(
          `SELECT DISTINCT a.id, a.name, a.canonical_name
           FROM qa_evaluations e JOIN qa_agents a ON a.id = e.agent_id
           WHERE e.team_id = ? AND e.state = 'finalized'
             AND COALESCE(e.call_connected_at, e.created_at) >= ?
             AND COALESCE(e.call_connected_at, e.created_at) <= ?`
        )
        .bind(teamId, new Date(fromMs).toISOString(), new Date(toMs).toISOString())
        .all<any>()
    ).results;
    for (const agent of agents) {
      const existing = await db
        .prepare(
          `SELECT id FROM qa_assessments
           WHERE team_id = ? AND agent_id = ? AND is_current = 1
             AND range_start_at <= ? AND range_end_at >= ? LIMIT 1`
        )
        .bind(teamId, agent.id, new Date(toMs).toISOString(), new Date(fromMs).toISOString())
        .first<any>();
      if (existing) {
        skipped.push(`${teamId}/${agent.name}`);
        continue;
      }
      const facts = await agentFacts(db, config, agent, {
        fromMs, toMs, label: month,
      });
      if (!facts) continue;
      const { system, prompt } = buildProgressionPrompts(config, facts);
      items.push({
        ref: {
          kind: "eom",
          team_id: teamId,
          agent_id: agent.id,
          agent_name: agent.canonical_name || agent.name,
          days,
          range_start_at: new Date(fromMs).toISOString(),
          range_end_at: new Date(toMs).toISOString(),
          evaluations_included: facts.evaluations.n,
          rubric_version: config.rubric_version,
        },
        system,
        prompt,
      });
    }
  }
  if (!items.length) return { triggered: false, items: 0, skipped: skipped.length };
  const trigger = await triggerInsights(db, request, {
    mode: "eom_batch",
    model: { model: INSIGHTS_MODEL, max_tokens: 3000 },
    items,
  });
  return {
    triggered: trigger.ok,
    items: items.length,
    skipped: skipped.length,
    ...(trigger.ok ? { run_id: trigger.runId } : { error: trigger.detail }),
  };
}

// ── team insight (CL5) — "what should we coach most" ───────────────────────
// Surfaced on the /coaching page (the §11.2 home for coach-facing tools).

export function buildTeamPrompts(teamId: string, facts: any) {
  const system =
    "You are a QA program analyst for Landing's call teams, advising the " +
    "human trainers and supervisors who run coaching. You are given a " +
    "deterministic fact sheet computed from the QA database. Identify the " +
    "areas of opportunity the team should tackle MOST, judge whether " +
    "coaching is working (commitment met-rate, post-coaching movement), and " +
    "call out program hygiene problems (overdue confirmations, uncoached " +
    "agents). The fact sheet's `tags` block is the typed theme vocabulary " +
    "(supertag → tag → subtag, session counts ROLLED UP to ancestors): a " +
    "theme coached across MANY agents with a low met-rate or flat/negative " +
    "post-coaching delta is a FLOOR-WIDE TRAINING PROGRAM candidate; a " +
    "theme confined to one or two agents is individual re-coaching, not a " +
    "program. Cite the figures you use; never invent numbers. Be direct.";
  const prompt =
    `TEAM FACT SHEET (${teamId}):\n${JSON.stringify(facts, null, 2)}\n\n` +
    `Respond with ONLY a JSON object (no fences, no prose outside it):\n` +
    `{"narrative": "<3-6 sentences: is coaching moving the numbers, and ` +
    `what matters most now>",\n` +
    ` "top_priorities": [{"title": "<short imperative>", "scope": ` +
    `"program"|"individual", "why": "<1-2 sentences with figures from the ` +
    `facts — name the tag themes involved>"}]}\n` +
    `Give 2-4 priorities, most important first. Use scope "program" only ` +
    `when the tag evidence spans several agents.`;
  return { system, prompt };
}

export async function teamInsightRequest(
  request: Request,
  db: D1Database,
  teamId: string,
  url: URL,
  lookupAllow?: string
): Promise<Response> {
  const access = await resolveAccess(request, db, lookupAllow);
  if (!canCoach(access, teamId))
    return json({ detail: "Team insights are restricted to QA staff and team managers." }, 403);
  const days = Math.min(365, Math.max(14, parseInt(url.searchParams.get("days") ?? "90", 10) || 90));
  const jobId = `insights-team-${teamId}-${days}`;

  const current = await db
    .prepare(
      `SELECT * FROM qa_coaching_insights
       WHERE team_id = ? AND scope = 'team' AND is_current = 1
       ORDER BY generated_at DESC LIMIT 1`
    )
    .bind(teamId)
    .first<any>();
  const fresh = current && Date.now() - Date.parse(current.generated_at) < FRESH_MS;
  const existing = await db
    .prepare("SELECT status, result FROM workflow_runs WHERE run_id = ?")
    .bind(jobId)
    .first<any>();
  const inFlight = existing && ["queued", "pending", "running"].includes(existing.status);

  const shape = (row: any) => ({
    ready: true,
    narrative: row.narrative,
    facts: row.facts ? JSON.parse(row.facts) : null,
    generated_at: row.generated_at,
    window_days: row.window_start && row.window_end
      ? Math.round((Date.parse(row.window_end) - Date.parse(row.window_start)) / 86_400_000)
      : null,
  });

  if (request.method === "GET") {
    if (inFlight) return json({ ready: false, job_id: jobId, status: "pending" }, 202);
    if (current) return json(shape(current));
    if (existing?.status === "error") {
      let note = "generation failed";
      try { note = JSON.parse(existing.result)?.note ?? note; } catch {}
      return json({ ready: false, status: "error", detail: note });
    }
    return json({ ready: false, status: "none" });
  }

  // POST — regenerate (fresh current row within the hour → serve, no spend).
  if (fresh) return json(shape(current));
  if (inFlight) return json({ ready: false, job_id: jobId, status: "pending", deduped: true }, 202);

  const config = await loadTeamConfig(db, teamId);
  const names: Record<string, string> = {};
  for (const s of config.sections_by_number) names[s.id] = s.name;
  const facts = await teamFacts(db, teamId, names, days);
  if (!facts.coaching.sessions)
    return json({ detail: `No coaching sessions on ${teamId} in the last ${days} days — nothing to analyze yet.` }, 404);
  const { system, prompt } = buildTeamPrompts(teamId, facts);
  const ref = {
    kind: "team",
    team_id: teamId,
    days,
    window_start: new Date(Date.now() - days * 86_400_000).toISOString(),
    window_end: new Date().toISOString(),
    facts,
    job_id: jobId,
  };
  const trigger = await triggerInsights(db, request, {
    mode: "team",
    model: { model: INSIGHTS_MODEL, max_tokens: 2000 },
    items: [{ ref, system, prompt }],
  });
  if (!trigger.ok)
    return json({ ready: false, status: "busy", detail: trigger.detail }, 409);
  await db
    .prepare(
      "INSERT INTO workflow_runs (run_id, workflow_name, status, result, created_at) VALUES (?, ?, 'running', ?, strftime('%Y-%m-%dT%H:%M:%fZ','now')) " +
        "ON CONFLICT(run_id) DO UPDATE SET status='running', result=excluded.result"
    )
    .bind(jobId, WORKFLOW_NAME, JSON.stringify({ sandy_run_id: trigger.runId }))
    .run();
  return json({ ready: false, job_id: jobId, status: "pending" }, 202);
}

// ── callback persist ───────────────────────────────────────────────────────

export async function insightsCallback(
  body: any,
  db: D1Database
): Promise<{ ok: boolean; note: string }> {
  const items: any[] = body.items_out ?? [];
  let persisted = 0;
  const errors: string[] = [];
  for (const item of items) {
    const ref = item.ref ?? {};
    try {
      if (!item.ok) throw new Error(item.error ?? "item failed");
      if (ref.kind === "team") {
        const parsed = parseItemJson(item.text ?? "");
        if (!parsed?.narrative)
          throw new Error("response is not the expected JSON shape");
        const prios = Array.isArray(parsed.top_priorities) ? parsed.top_priorities : [];
        // One displayable text — narrative + numbered priorities; the input
        // fact sheet persists beside it so every figure stays checkable.
        const narrative =
          String(parsed.narrative) +
          (prios.length
            ? "\n\nTOP PRIORITIES:\n" +
              prios
                .map((p: any, i: number) =>
                  `${i + 1}. ${p.scope ? `[${String(p.scope).toUpperCase()}] ` : ""}${p.title} — ${p.why}`)
                .join("\n")
            : "");
        await db
          .prepare(
            "UPDATE qa_coaching_insights SET is_current = 0 WHERE team_id = ? AND scope = 'team' AND is_current = 1"
          )
          .bind(ref.team_id)
          .run();
        await db
          .prepare(
            `INSERT INTO qa_coaching_insights (scope, team_id, window_start,
              window_end, facts, narrative, models_used)
             VALUES ('team', ?, ?, ?, ?, ?, ?)`
          )
          .bind(
            ref.team_id, ref.window_start, ref.window_end,
            JSON.stringify(ref.facts ?? {}), narrative,
            JSON.stringify({
              text: { provider: "anthropic", model: item.model ?? INSIGHTS_MODEL },
              usage: item.usage ?? null,
              mode: "team",
            })
          )
          .run();
        persisted++;
        if (ref.job_id) {
          await db
            .prepare("UPDATE workflow_runs SET status='complete', result=? WHERE run_id=?")
            .bind(JSON.stringify({ ok: true, scope: "team" }), ref.job_id)
            .run();
        }
        continue;
      }
      if (!["progression", "eom"].includes(ref.kind))
        throw new Error(`unknown ref kind '${ref.kind}'`);
      const config = await loadTeamConfig(db, ref.team_id);
      const parsed = parseItemJson(item.text ?? "");
      if (!parsed?.overall_assessment || !Array.isArray(parsed.sections))
        throw new Error("response is not the expected JSON shape");
      const expected = assessedSections(config);
      const byId = new Map(parsed.sections.map((s: any) => [s.section_id, s]));
      const rows = expected.map((s) => {
        const out: any = byId.get(s.id);
        if (!out) throw new Error(`missing section '${s.id}'`);
        if (!["improving", "stable", "declining"].includes(out.trend))
          throw new Error(`section '${s.id}': bad trend '${out.trend}'`);
        if (!out.summary || !out.coaching_tip)
          throw new Error(`section '${s.id}': summary/coaching_tip required`);
        return {
          section_id: s.id,
          section_name: s.name,
          section_number: s.section_number,
          trend: out.trend,
          summary: String(out.summary),
          coaching_tip: String(out.coaching_tip),
        };
      });
      const fvRow = await db
        .prepare(
          "SELECT formula_version FROM qa_formula_versions WHERE team_id = ? AND effective_until IS NULL ORDER BY effective_from DESC LIMIT 1"
        )
        .bind(ref.team_id)
        .first<any>();
      // High-range id (Railway-parity table — the CL0 doctrine).
      const idRow = await db
        .prepare("SELECT COALESCE(MAX(id) + 1, ?) AS v FROM qa_assessments WHERE id >= ?")
        .bind(SANDY_BASE, SANDY_BASE)
        .first<any>();
      const aid = idRow?.v ?? SANDY_BASE;
      await db
        .prepare(
          "UPDATE qa_assessments SET is_current = 0 WHERE team_id = ? AND agent_id = ? AND time_range_days = ? AND is_current = 1"
        )
        .bind(ref.team_id, ref.agent_id, ref.days)
        .run();
      await db
        .prepare(
          `INSERT INTO qa_assessments (id, agent_id, team_id, time_range_days,
            range_start_at, range_end_at, evaluations_included, overall_assessment,
            rubric_version, formula_version, models_used, is_current)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,1)`
        )
        .bind(
          aid, ref.agent_id, ref.team_id, ref.days,
          ref.range_start_at, ref.range_end_at, ref.evaluations_included,
          String(parsed.overall_assessment),
          ref.rubric_version, fvRow?.formula_version ?? null,
          JSON.stringify({
            text: { provider: "anthropic", model: item.model ?? INSIGHTS_MODEL },
            usage: item.usage ?? null,
            mode: ref.kind,
          })
        )
        .run();
      for (const r of rows) {
        // Explicit high-range id (aid*100+n, the eval-sections pattern) —
        // autoincrement handed Sandy-born sections LOW ids (554+) that
        // PG's own serial then collided with, wedging the shadow sync's
        // assessments reimport from 2026-08-20 on. Sandy-born rows in a
        // Railway-parity table must own an id space PG can never reach.
        await db
          .prepare(
            `INSERT INTO qa_assessment_sections (id, assessment_id, section_id,
              section_name, section_number, trend, summary, coaching_tip)
             VALUES (?,?,?,?,?,?,?,?)`
          )
          .bind(aid * 100 + r.section_number, aid, r.section_id, r.section_name, r.section_number, r.trend, r.summary, r.coaching_tip)
          .run();
      }
      persisted++;
      if (ref.job_id) {
        await db
          .prepare("UPDATE workflow_runs SET status='complete', result=? WHERE run_id=?")
          .bind(JSON.stringify({ ok: true, assessment_id: aid }), ref.job_id)
          .run();
      }
    } catch (err) {
      const msg = `${ref.team_id ?? "?"}/${ref.agent_name ?? "?"}: ${String((err as any)?.message ?? err).slice(0, 200)}`;
      errors.push(msg);
      if (ref.job_id) {
        try {
          await db
            .prepare("UPDATE workflow_runs SET status='error', result=? WHERE run_id=?")
            .bind(JSON.stringify({ ok: false, note: msg }), ref.job_id)
            .run();
        } catch {}
      }
    }
  }
  return {
    ok: errors.length === 0,
    note: `persisted ${persisted}/${items.length} assessment(s)` +
      (errors.length ? ` — errors: ${errors.join(" | ")}` : ""),
  };
}

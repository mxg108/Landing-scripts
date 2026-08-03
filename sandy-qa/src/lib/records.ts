// EvaluationRecord read path — worker-side port of backend/services/
// db_provider.py (PostgresProvider). Serves the datapoint page, the
// per-agent dashboard, and the drill-downs with SECTION content, shaped
// exactly like the Railway provider (sections keyed by history_id,
// manager_email/improvements naming bridges, YN_DISPLAY strings).

import type { TeamConfig } from "./teamConfig.js";

const YN_DISPLAY: Record<string, string> = {
  Y: "Yes",
  N: "No",
  NA: "Not Applicable",
};

const EVAL_COLUMNS = `id, agent_name_raw, agent_email, evaluator_email,
  COALESCE(call_connected_at, created_at) AS ts,
  overall_score, dialpad_link, key_strengths, opportunities,
  call_summary, caller_name, caller_phone, source,
  dialpad_disposition_category, dialpad_disposition, ai_csat,
  call_duration_ms, dialpad_call_id, dialpad_entry_point_call_id,
  dialpad_master_call_id, dialpad_call_metadata`;

export function extractEvalId(link: string): string {
  if (!link) return "";
  const clean = link.split("[")[0].trim().split("?")[0].trim();
  return clean.replace(/\/+$/, "").split("/").pop() ?? "";
}

function isoZ(ts: string): string {
  const t = Date.parse(ts.includes("T") ? ts : ts.replace(" ", "T") + "Z");
  return new Date(t).toISOString();
}

function displayScore(row: any | undefined): string {
  if (!row) return "";
  if (row.numeric_score !== null && row.numeric_score !== undefined)
    return String(row.numeric_score);
  if (row.binary_value !== null && row.binary_value !== undefined)
    return YN_DISPLAY[row.binary_value] ?? "Not Applicable";
  return "";
}

function recordFromRows(config: TeamConfig, ev: any, sectionRows: any[]): any {
  const bySectionId = new Map(sectionRows.map((s) => [s.section_id, s]));
  const sections: Record<string, any> = {};
  for (const sec of config.sections_by_number) {
    const row = bySectionId.get(sec.id);
    sections[sec.history_id] = {
      score: displayScore(row),
      confidence: row?.confidence ?? null,
      reasoning: row?.reasoning ?? null,
    };
  }
  let metadata: any = {};
  if (ev.dialpad_call_metadata) {
    try {
      metadata = JSON.parse(ev.dialpad_call_metadata);
    } catch {}
  }
  if (typeof metadata !== "object" || metadata === null) metadata = {};
  const link = ev.dialpad_link || null;
  return {
    timestamp: isoZ(ev.ts),
    agent_name: ev.agent_name_raw,
    agent_email: ev.agent_email ?? "",
    manager_email: ev.evaluator_email ?? "",
    overall_score: ev.overall_score !== null ? Number(ev.overall_score) : 0.0,
    sections,
    eval_id: extractEvalId(link ?? ""),
    dialpad_link: link,
    key_strengths: ev.key_strengths || null,
    improvements: ev.opportunities || null,
    call_summary: ev.call_summary || null,
    caller_name: ev.caller_name || null,
    caller_phone: ev.caller_phone || null,
    source: ev.source || "manual",
    dialpad_disposition_category: ev.dialpad_disposition_category || null,
    dialpad_disposition: ev.dialpad_disposition || null,
    ai_csat: ev.ai_csat !== null && ev.ai_csat !== undefined ? Number(ev.ai_csat) : null,
    call_duration_ms: ev.call_duration_ms ?? null,
    dialpad_call_id: ev.dialpad_call_id || null,
    dialpad_entry_point_call_id: ev.dialpad_entry_point_call_id || null,
    dialpad_master_call_id: ev.dialpad_master_call_id || null,
    sop_used: metadata.sop_used || null,
    pulpo_docs: Array.isArray(metadata.pulpo_docs) ? metadata.pulpo_docs : [],
  };
}

async function attachSections(
  db: D1Database,
  config: TeamConfig,
  evals: any[]
): Promise<any[]> {
  if (!evals.length) return [];
  const ids = evals.map((e) => e.id);
  // D1 has no array binds — chunk an IN list (evals per window are bounded).
  const sectionRows: any[] = [];
  for (let i = 0; i < ids.length; i += 80) {
    const chunk = ids.slice(i, i + 80);
    const res = await db
      .prepare(
        `SELECT evaluation_id, section_id, numeric_score, binary_value, confidence, reasoning
         FROM qa_evaluation_sections WHERE evaluation_id IN (${chunk.map(() => "?").join(",")})`
      )
      .bind(...chunk)
      .all<any>();
    sectionRows.push(...res.results);
  }
  const byEval = new Map<number, any[]>();
  for (const s of sectionRows) {
    let g = byEval.get(s.evaluation_id);
    if (!g) byEval.set(s.evaluation_id, (g = []));
    g.push(s);
  }
  return evals.map((ev) => recordFromRows(config, ev, byEval.get(ev.id) ?? []));
}

export async function fetchRecords(
  db: D1Database,
  config: TeamConfig,
  opts: { days?: number; agentRaw?: string; fromMs?: number; toMs?: number }
): Promise<any[]> {
  let where =
    "WHERE team_id = ? AND state = 'finalized'";
  const params: any[] = [config.team_id];
  if (opts.fromMs !== undefined && opts.toMs !== undefined) {
    where += " AND COALESCE(call_connected_at, created_at) >= ? AND COALESCE(call_connected_at, created_at) <= ?";
    params.push(new Date(opts.fromMs).toISOString(), new Date(opts.toMs).toISOString());
  } else if (opts.days !== undefined) {
    const cutoff = new Date(Date.now() - opts.days * 86_400_000).toISOString();
    where += " AND COALESCE(call_connected_at, created_at) >= ?";
    params.push(cutoff);
  }
  if (opts.agentRaw !== undefined) {
    where += " AND LOWER(agent_name_raw) = LOWER(?)";
    params.push(opts.agentRaw.trim());
  }
  const evals = await db
    .prepare(
      `SELECT ${EVAL_COLUMNS} FROM qa_evaluations ${where}
       ORDER BY COALESCE(call_connected_at, created_at)`
    )
    .bind(...params)
    .all<any>();
  return attachSections(db, config, evals.results);
}

export async function getByEvalId(
  db: D1Database,
  config: TeamConfig,
  callId: string
): Promise<any | null> {
  if (!callId) return null;
  const row = await db
    .prepare(
      `SELECT ${EVAL_COLUMNS} FROM qa_evaluations
       WHERE team_id = ? AND state = 'finalized'
       AND (dialpad_entry_point_call_id = ? OR dialpad_call_id = ?)
       ORDER BY COALESCE(call_connected_at, created_at) LIMIT 1`
    )
    .bind(config.team_id, callId, callId)
    .first<any>();
  if (row) {
    const [rec] = await attachSections(db, config, [row]);
    if (rec.eval_id === callId) return rec;
  }
  // Fallback scan (junk-link rows) — mirrors the route's 365-day scan.
  const all = await fetchRecords(db, config, { days: 365 });
  return all.find((r) => r.eval_id === callId) ?? null;
}

export async function listAgents(db: D1Database, teamId: string): Promise<string[]> {
  const rows = await db
    .prepare(
      `SELECT DISTINCT agent_name_raw FROM qa_evaluations
       WHERE team_id = ? AND state = 'finalized' ORDER BY agent_name_raw`
    )
    .bind(teamId)
    .all<{ agent_name_raw: string }>();
  return rows.results.map((r) => r.agent_name_raw.trim()).filter(Boolean);
}

export async function resolveTeamForAgent(
  db: D1Database,
  email: string
): Promise<string | null> {
  const row = await db
    .prepare(
      `SELECT team_id FROM qa_agents WHERE active = 1 AND LOWER(email) = LOWER(?)
       ORDER BY CASE team_id WHEN 'member_support' THEN 0 ELSE 1 END LIMIT 1`
    )
    .bind(email.trim())
    .first<{ team_id: string }>();
  return row?.team_id ?? null;
}

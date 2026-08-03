// The analytics "history frame" — worker-side port of
// backend/services/team_source.py (frame_from_rows + fetch_history_frame +
// _section_alias_map + _roster_maps). One row per finalized evaluation, wide
// form; numeric sections null when missing (pandas NaN), yn sections ''.

import type { TeamConfig } from "./teamConfig.js";

export interface FrameRow {
  agent: string;
  ts: number; // epoch ms (naive-UTC wall clock)
  eval_approved_at: number | null;
  overall_score: number;
  manager_email: string;
  is_active: boolean;
  supervisor: string;
  eval_id: string;
  num: Record<string, number | null>;
  yn: Record<string, string>;
}

export function stripAccents(s: string): string {
  return s.normalize("NFKD").replace(/\p{M}/gu, "");
}

function evalIdFromLink(link: string): string {
  if (!link) return "";
  const clean = link.split("[")[0].trim().split("?")[0].trim();
  return clean.replace(/\/+$/, "").split("/").pop() ?? "";
}

// D1 TEXT timestamps are ISO-8601 UTC ("...Z"). Parse to epoch ms.
function parseTs(v: string | null): number | null {
  if (!v) return null;
  const t = Date.parse(v.includes("T") ? v : v.replace(" ", "T") + "Z");
  return Number.isNaN(t) ? null : t;
}

function aliasMap(config: TeamConfig): Map<string, string> {
  const alias = new Map<string, string>();
  for (const s of config.sections_by_number) {
    alias.set(s.history_id, s.history_id);
    alias.set(s.id, s.history_id);
  }
  const currentByNumber = new Map(
    config.sections_by_number.map((s) => [s.section_number, s.history_id])
  );
  for (const rubric of config.archived_rubrics) {
    for (const s of rubric.sections ?? []) {
      const target = currentByNumber.get(s.section_number);
      if (target === undefined) continue;
      if (!alias.has(s.id)) alias.set(s.id, target);
      const hid = s.history_id ?? s.id;
      if (!alias.has(hid)) alias.set(hid, target);
    }
  }
  return alias;
}

export async function fetchHistoryFrame(
  db: D1Database,
  config: TeamConfig
): Promise<FrameRow[]> {
  const [evals, sections, agents] = await Promise.all([
    db
      .prepare(
        `SELECT e.id, e.agent_id, e.agent_name_raw, e.evaluator_email, e.overall_score,
                e.dialpad_link, e.dialpad_entry_point_call_id, e.dialpad_call_metadata,
                COALESCE(e.call_connected_at, e.approved_at) AS ts, e.approved_at,
                COALESCE(a.canonical_name, a.name, e.agent_name_raw) AS agent_display,
                COALESCE(a.active, 0) AS is_active,
                COALESCE(a.supervisor_email, '') AS supervisor
         FROM qa_evaluations e LEFT JOIN qa_agents a ON a.id = e.agent_id
         WHERE e.team_id = ? AND e.state = 'finalized' ORDER BY e.id`
      )
      .bind(config.team_id)
      .all<any>(),
    db
      .prepare(
        "SELECT evaluation_id, section_id, numeric_score, binary_value FROM qa_v_history_long WHERE team_id = ?"
      )
      .bind(config.team_id)
      .all<any>(),
    db
      .prepare(
        "SELECT name, canonical_name, active, supervisor_email FROM qa_agents WHERE team_id = ?"
      )
      .bind(config.team_id)
      .all<any>(),
  ]);

  const numericIds = new Set(config.numeric_history_ids);
  const ynIds = new Set(config.yn_history_ids);
  const excluded = new Set(
    config.excluded_test_agents.map((a) => a.trim().toLowerCase())
  );
  const alias = aliasMap(config);

  // roster maps from ACTIVE agents (read-time identity fallback)
  const canonicalMap = new Map<string, string>();
  const supervisorMap = new Map<string, string>();
  const activeSet = new Set<string>();
  for (const a of agents.results) {
    if (!a.active) continue;
    const name = (a.name ?? "").trim();
    if (!name) continue;
    const canonical = (a.canonical_name ?? "").trim() || name;
    canonicalMap.set(name.toLowerCase(), canonical);
    canonicalMap.set(stripAccents(name).toLowerCase(), canonical);
    canonicalMap.set(canonical.toLowerCase(), canonical);
    canonicalMap.set(stripAccents(canonical).toLowerCase(), canonical);
    supervisorMap.set(canonical.toLowerCase(), a.supervisor_email ?? "");
    activeSet.add(canonical.toLowerCase());
  }

  // Section values per evaluation, keyed by resolved history_id.
  //
  // Collision rule: an archived rubric can position-alias an old id into a
  // column that current-id rows ALSO write (observed: sales_v1 value_uplift@6
  // → sales_v2 landing_guarantee@6, while v1 evals carry a real
  // landing_guarantee row too). Python's last-write-wins outcome depends on
  // Postgres row order (insertion order → the DIRECT row lands last and
  // wins). Encode that outcome explicitly: direct current-id matches always
  // beat position-aliased writes; aliased writes never overwrite anything.
  const currentIds = new Set(config.sections_by_number.map((s) => s.id));
  const byEval = new Map<number, Record<string, number | string | null>>();
  const directWrite = new Map<number, Set<string>>();
  for (const s of sections.results) {
    const hid = alias.get(s.section_id);
    if (hid === undefined || (!numericIds.has(hid) && !ynIds.has(hid))) continue;
    const val = numericIds.has(hid)
      ? s.numeric_score !== null
        ? Number(s.numeric_score)
        : null // pandas NaN
      : s.binary_value ?? "";
    const isDirect = currentIds.has(s.section_id);
    let rec = byEval.get(s.evaluation_id);
    if (!rec) {
      byEval.set(s.evaluation_id, (rec = {}));
      directWrite.set(s.evaluation_id, new Set());
    }
    const directs = directWrite.get(s.evaluation_id)!;
    if (hid in rec) {
      if (!isDirect || directs.has(hid)) continue; // aliased never overwrites
    }
    rec[hid] = val;
    if (isDirect) directs.add(hid);
  }

  const rows: FrameRow[] = [];
  for (const ev of evals.results) {
    let agent = (ev.agent_display ?? ev.agent_name_raw ?? "").trim();
    let isActive = !!ev.is_active;
    let supervisor = ev.supervisor ?? "";
    if (ev.agent_id === null && agent) {
      const canonical =
        canonicalMap.get(agent.toLowerCase()) ??
        canonicalMap.get(stripAccents(agent).toLowerCase());
      if (canonical !== undefined) {
        agent = canonical;
        isActive = activeSet.has(canonical.toLowerCase());
        supervisor = supervisorMap.get(canonical.toLowerCase()) ?? "";
      }
    }
    if (!agent || excluded.has(agent.toLowerCase())) continue;
    const ts = parseTs(ev.ts);
    if (ts === null || new Date(ts).getUTCFullYear() < 2020) continue;
    const meta = ev.dialpad_call_metadata ? JSON.parse(ev.dialpad_call_metadata) : {};
    const link =
      ev.dialpad_link || meta?.backfill?.superseded_dialpad_link || "";
    const evalId = evalIdFromLink(link) || (ev.dialpad_entry_point_call_id ?? "");

    const secVals = byEval.get(ev.id) ?? {};
    const num: Record<string, number | null> = {};
    for (const hid of config.numeric_history_ids)
      num[hid] = (secVals[hid] as number | null | undefined) ?? null;
    const yn: Record<string, string> = {};
    for (const hid of config.yn_history_ids)
      yn[hid] = (secVals[hid] as string | undefined) ?? "";

    rows.push({
      agent,
      ts,
      eval_approved_at: parseTs(ev.approved_at),
      overall_score: Number(ev.overall_score),
      manager_email: (ev.evaluator_email ?? "").toLowerCase(),
      is_active: isActive,
      supervisor,
      eval_id: evalId,
      num,
      yn,
    });
  }
  return rows;
}

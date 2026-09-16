// Nightly disposition sweep (NightlyScoring.md, owner sign-off 2026-08-30;
// CronContinuation.md §2.4 for the 2026-09-16 step refactor).
// — rides the "7 * * * *" cron + the qa-cron-ticker workflow (Sandy caps
// at 2 schedules; no new slot possible). Replaces the manual daily
// selection routine for Member Support: pull yesterday's dispositions
// records export from the Dialpad Stats API, fill D1 dispositions
// (UPDATE-only during shadow — §2.1, the AA0/CL0 sync-collision class),
// select up to per_agent calls per active roster agent (random,
// distinct-disposition preference), and enqueue them through the normal
// scoring path with emails suppressed.
//
// State machine: one qa_disposition_pulls row per (team, local day) is the
// re-run latch, the resume handle, and the audit trail. Since the Sandy
// scheduler started bounding cron dispatch time (2026-09-14) the sweep no
// longer runs in one tick: every call to sweepDispositions performs ONE
// bounded phase transition (≤ a few seconds) and reports `more` while
// work remains; the ticker workflow calls again ~15 s later. Phases:
//   initiate → poll → fill (200 rows/step) → select → enqueue (3/step) → done
// Every phase resumes exactly from `cursor`; a cut step re-does at most
// one chunk (fill is wins-once, enqueue is 409-idempotent). All failures
// degrade to a report, never a throw — the cron handler stays up.

import type { StatsContext } from "../routes/scoring.js";
import {
  initiateExport as initiateStatsExport,
  localDay,
  naiveLocalToIso,
  parseCsv,
  pollErrorStatus,
  pollOnce,
  type FetchLike,
} from "./dialpadStats.js";

// SR1 (ShiftReport §10.3): the Stats client + CSV/tz helpers now live in
// dialpadStats.ts; re-exported so existing importers/tests keep working.
export { localDay, naiveLocalToIso, parseCsv } from "./dialpadStats.js";

// Sandy-born id space (PR #174) — the evals disposition back-fill is scoped
// to rows the shadow sync can never fill (Railway fills its own via PG).
const SANDY_BASE = 10_000_000;
const CATCHUP_WINDOW_HOURS = 6; // §6: retries until local_hour_utc + 6
const ID_CHUNK = 20; // triple-key + link = 4 params/id; D1 param cap ~100
export const FILL_ROWS_PER_STEP = 200; // 5 D1 batches of 80 statements
export const ENQUEUES_PER_STEP = 3; // each ≈ 1–2 s (provider meta fetch + 6 queries)
const MAX_REINITS = 3; // expired export ids re-initiated on the spot, then error

export interface NightlySweepConfig {
  enabled?: boolean;
  per_agent?: number;
  min_duration_s?: number;
  max_duration_s?: number;
  suppress_email?: boolean;
  reviewer_email?: string;
  timezone?: string;
  local_hour_utc?: number;
  max_enqueues?: number;
}

export interface StatsRow {
  call_id: string;
  disposition_category: string | null;
  disposition: string | null;
  operator_email: string | null;
  direction: string | null;
  recording_url: string | null;
  connected_at: string | null; // ISO UTC
  ended_at: string | null; // ISO UTC
  duration_s: number | null;
}

// A selected call, persisted in qa_disposition_pulls.picks between the
// select and enqueue phases.
export interface Pick {
  call_id: string;
  agent_email: string;
  disposition_category: string | null;
  disposition: string | null;
  connected_at: string | null;
  ended_at: string | null;
}

// ── pure helpers (harness-tested) ──────────────────────────────────────────

// `Category~Subdisposition` → pair; bare category → (category, null).
// (Port of disposition_pull.py::split_disposition.)
export function splitDisposition(label: string): [string | null, string | null] {
  const trimmed = (label || "").trim();
  if (!trimmed) return [null, null];
  const idx = trimmed.indexOf("~");
  if (idx < 0) return [trimmed, null];
  const category = trimmed.slice(0, idx).trim();
  const sub = trimmed.slice(idx + 1).trim();
  return [category || null, sub || null];
}

// Rows without a call_id drop (unjoinable); rows without a disposition are
// KEPT — they matter for selection (absence is a designed grounding state)
// and their cc row may gain a disposition on a later pull.
export function parseExportCsv(text: string): StatsRow[] {
  const rows = parseCsv(text);
  if (!rows.length) return [];
  const header = rows[0].map((h) => h.trim());
  const col = (name: string) => header.indexOf(name);
  const iCall = col("call_id");
  if (iCall < 0) return [];
  const idx = {
    disposition: col("disposition"),
    email: col("operator_email"),
    direction: col("direction"),
    recording: col("recording_url"),
    connected: col("date_connected"),
    ended: col("date_ended"),
    tz: col("timezone"),
  };
  const out: StatsRow[] = [];
  for (const r of rows.slice(1)) {
    const callId = (r[iCall] ?? "").trim();
    if (!callId) continue;
    const tz = idx.tz >= 0 ? (r[idx.tz] ?? "").trim() || "UTC" : "UTC";
    const [category, sub] = splitDisposition(idx.disposition >= 0 ? r[idx.disposition] : "");
    const connected = idx.connected >= 0 ? naiveLocalToIso(r[idx.connected] ?? "", tz) : null;
    const ended = idx.ended >= 0 ? naiveLocalToIso(r[idx.ended] ?? "", tz) : null;
    const duration =
      connected && ended
        ? Math.round((Date.parse(ended) - Date.parse(connected)) / 1000)
        : null;
    out.push({
      call_id: callId,
      disposition_category: category,
      disposition: sub,
      operator_email: idx.email >= 0 ? (r[idx.email] ?? "").trim().toLowerCase() || null : null,
      direction: idx.direction >= 0 ? (r[idx.direction] ?? "").trim() || null : null,
      recording_url: idx.recording >= 0 ? (r[idx.recording] ?? "").trim() || null : null,
      connected_at: connected,
      ended_at: ended,
      duration_s: duration,
    });
  }
  return dedupeRows(out);
}

// One row per call_id (a multi-row fill cannot touch the same row twice).
// Last wins — except a dispositioned record is never replaced by an
// undispositioned one. (Port of disposition_pull.py::dedupe_records.)
export function dedupeRows(rows: StatsRow[]): StatsRow[] {
  const byId = new Map<string, StatsRow>();
  for (const r of rows) {
    const prev = byId.get(r.call_id);
    if (prev && prev.disposition_category && !r.disposition_category) continue;
    byId.set(r.call_id, r);
  }
  return [...byId.values()];
}

// §4: random with preference for distinct disposition categories. One
// random pick per distinct non-NULL category (categories visited in random
// order) until `slots`; remainder (NULL-category rows included) fills
// randomly. rng injectable so the harness can pin it.
export function pickForAgent(
  rows: StatsRow[],
  slots: number,
  rng: () => number
): StatsRow[] {
  if (slots <= 0 || !rows.length) return [];
  const shuffle = <T>(a: T[]): T[] => {
    const arr = [...a];
    for (let i = arr.length - 1; i > 0; i--) {
      const j = Math.floor(rng() * (i + 1));
      [arr[i], arr[j]] = [arr[j], arr[i]];
    }
    return arr;
  };
  const byCat = new Map<string, StatsRow[]>();
  for (const r of rows) {
    if (!r.disposition_category) continue;
    const list = byCat.get(r.disposition_category) ?? [];
    list.push(r);
    byCat.set(r.disposition_category, list);
  }
  const picked: StatsRow[] = [];
  const taken = new Set<string>();
  for (const cat of shuffle([...byCat.keys()])) {
    if (picked.length >= slots) break;
    const pool = byCat.get(cat)!;
    const pick = pool[Math.floor(rng() * pool.length)];
    picked.push(pick);
    taken.add(pick.call_id);
  }
  if (picked.length < slots) {
    for (const r of shuffle(rows.filter((r) => !taken.has(r.call_id)))) {
      if (picked.length >= slots) break;
      picked.push(r);
      taken.add(r.call_id);
    }
  }
  return picked;
}

// True when any team's last step reported more work — the ticker's loop
// condition (CronContinuation §2.3).
export function sweepHasMore(out: Record<string, any> | null | undefined): boolean {
  if (!out) return false;
  return Object.values(out).some((r: any) => r && r.more === true);
}

// ── D1 fills (§5.2 — UPDATE-only during shadow) ────────────────────────────
// Constraints (NightlyScoring §2): never INSERT cc_calls rows (sync id/uq
// collision class), never touch last_updated_at (the sync watermark is
// max(last_updated_at) OVER D1 — bumping it makes the sync skip PG rows),
// wins-once via disposition_source IS NULL.

async function fillDispositions(
  db: D1Database,
  teamId: string,
  rows: StatsRow[]
): Promise<{ fill_updated: number; evals_backfilled: number }> {
  const dispositioned = rows.filter((r) => r.disposition_category);
  let updated = 0;
  let evals = 0;
  const ccStmt = db.prepare(
    `UPDATE cc_calls SET disposition_category = ?, disposition = ?, disposition_source = 'stats_pull'
     WHERE team_id = ?
       AND (dialpad_call_id = ? OR dialpad_entry_point_call_id = ? OR dialpad_master_call_id = ?)
       AND disposition_source IS NULL`
  );
  const evalStmt = db.prepare(
    `UPDATE qa_evaluations SET dialpad_disposition_category = ?, dialpad_disposition = ?
     WHERE team_id = ? AND id >= ${SANDY_BASE} AND dialpad_disposition_category IS NULL
       AND (dialpad_call_id = ? OR dialpad_entry_point_call_id = ? OR dialpad_master_call_id = ?
            OR dialpad_link = 'https://dialpad.com/callhistory/callreview/' || ?)`
  );
  for (let i = 0; i < dispositioned.length; i += 40) {
    const chunk = dispositioned.slice(i, i + 40);
    const results = await db.batch(
      chunk.flatMap((r) => [
        ccStmt.bind(r.disposition_category, r.disposition, teamId, r.call_id, r.call_id, r.call_id),
        evalStmt.bind(
          r.disposition_category, r.disposition, teamId,
          r.call_id, r.call_id, r.call_id, r.call_id
        ),
      ])
    );
    results.forEach((res, j) => {
      const changes = (res as any)?.meta?.changes ?? 0;
      if (j % 2 === 0) updated += changes;
      else evals += changes;
    });
  }
  return { fill_updated: updated, evals_backfilled: evals };
}

// Triple-key + review-link scored check, batched (manually-scored evals
// sometimes stored only per-leg ids — the CSV carries entry-point ids, and
// the link embeds the entry-point id verbatim; NightlyScoring §2.4).
async function attemptedCallIds(
  db: D1Database,
  teamId: string,
  callIds: string[]
): Promise<Set<string>> {
  const attempted = new Set<string>();
  for (let i = 0; i < callIds.length; i += ID_CHUNK) {
    const chunk = callIds.slice(i, i + ID_CHUNK);
    const marks = chunk.map(() => "?").join(",");
    const linkMarks = chunk.map(() => "'https://dialpad.com/callhistory/callreview/' || ?").join(",");
    const evalRows = await db
      .prepare(
        `SELECT dialpad_call_id AS a, dialpad_entry_point_call_id AS b, dialpad_master_call_id AS c, dialpad_link AS l
         FROM qa_evaluations WHERE team_id = ?
           AND (dialpad_call_id IN (${marks}) OR dialpad_entry_point_call_id IN (${marks})
                OR dialpad_master_call_id IN (${marks}) OR dialpad_link IN (${linkMarks}))`
      )
      .bind(teamId, ...chunk, ...chunk, ...chunk, ...chunk)
      .all<any>();
    const inChunk = new Set(chunk);
    for (const row of evalRows.results) {
      for (const key of [row.a, row.b, row.c]) if (key && inChunk.has(key)) attempted.add(key);
      const linkId = (row.l ?? "").split("/").pop();
      if (linkId && inChunk.has(linkId)) attempted.add(linkId);
    }
    // ANY prior queue row (incl. done/error) = the call had its one
    // automatic attempt (retellSweep doctrine — failures wait for a human).
    const queueRows = await db
      .prepare(`SELECT call_id FROM qa_score_queue WHERE team_id = ? AND call_id IN (${marks})`)
      .bind(teamId, ...chunk)
      .all<any>();
    for (const row of queueRows.results) attempted.add(row.call_id);
  }
  return attempted;
}

// ── the sweep ──────────────────────────────────────────────────────────────

export interface SweepTestOpts {
  nowMs?: number;
  rng?: () => number;
  fetchImpl?: FetchLike; // Dialpad Stats API stub (harness)
}

export async function sweepDispositions(
  db: D1Database,
  request: Request,
  env: {
    DIALPAD_API_KEY?: string;
    RETELL_API_KEY?: string;
    PULPO_MCP_URL?: string;
    PULPO_MCP_TOKEN?: string;
  },
  testOpts: SweepTestOpts = {}
): Promise<Record<string, any>> {
  if (!env.DIALPAD_API_KEY) return { skipped: "no_dialpad_key" };
  const teams = await db
    .prepare("SELECT id, provider_config FROM teams WHERE provider_config IS NOT NULL")
    .all<any>();
  const out: Record<string, any> = {};
  for (const team of teams.results) {
    let cfg: any = null;
    try {
      cfg = JSON.parse(team.provider_config);
    } catch {
      continue;
    }
    const sw: NightlySweepConfig = cfg?.nightly_sweep ?? {};
    if (!sw.enabled || !cfg?.callcenter_id) continue;
    try {
      out[team.id] = await sweepTeam(db, request, env, team.id, String(cfg.callcenter_id), sw, testOpts);
    } catch (err) {
      out[team.id] = { error: String((err as any)?.message ?? err).slice(0, 200), more: false };
    }
  }
  return out;
}

const parseJson = (text: string | null | undefined, fallback: any) => {
  if (!text) return fallback;
  try {
    return JSON.parse(text);
  } catch {
    return fallback;
  }
};

// One phase transition for one team (CronContinuation §2.4). Returns the
// step report with `more` = call again soon.
async function sweepTeam(
  db: D1Database,
  request: Request,
  env: { DIALPAD_API_KEY?: string },
  teamId: string,
  callcenterId: string,
  sw: NightlySweepConfig,
  testOpts: SweepTestOpts
): Promise<any> {
  const now = new Date(testOpts.nowMs ?? Date.now());
  const fetchImpl = testOpts.fetchImpl ?? fetch;
  const tz = sw.timezone ?? "America/Mexico_City";
  const localHourUtc = sw.local_hour_utc ?? 6;
  const nowIso = now.toISOString();

  // Resume any in-flight pull first (slow export, worker death mid-run,
  // a cut step) — every phase is idempotent, so re-walking is safe.
  let pull = await db
    .prepare(
      "SELECT * FROM qa_disposition_pulls WHERE team_id = ? AND status IN ('pending','fetching') ORDER BY pull_date LIMIT 1"
    )
    .bind(teamId)
    .first<any>();

  if (!pull) {
    const hour = now.getUTCHours();
    if (hour < localHourUtc || hour >= localHourUtc + CATCHUP_WINDOW_HOURS)
      return { skipped: "outside_window", more: false };
    const pullDate = localDay(tz, new Date(now.getTime() - 24 * 3600_000));
    const existing = await db
      .prepare("SELECT * FROM qa_disposition_pulls WHERE team_id = ? AND pull_date = ?")
      .bind(teamId, pullDate)
      .first<any>();
    if (existing?.status === "completed")
      return { skipped: "already_completed", pull_date: pullDate, more: false };
    if (existing?.status === "error") {
      // retry within the catch-up window with a FRESH export (same row)
      await db
        .prepare(
          `UPDATE qa_disposition_pulls SET status = 'pending', request_id = NULL, phase = NULL,
             cursor = 0, export_json = NULL, picks = NULL, reinits = 0, updated_at = ? WHERE id = ?`
        )
        .bind(nowIso, existing.id)
        .run();
      pull = { ...existing, status: "pending", request_id: null, phase: null, cursor: 0,
        export_json: null, picks: null, reinits: 0 };
    } else if (!existing) {
      await db
        .prepare(
          "INSERT INTO qa_disposition_pulls (team_id, pull_date) VALUES (?, ?) ON CONFLICT(team_id, pull_date) DO NOTHING"
        )
        .bind(teamId, pullDate)
        .run();
      pull = await db
        .prepare("SELECT * FROM qa_disposition_pulls WHERE team_id = ? AND pull_date = ?")
        .bind(teamId, pullDate)
        .first<any>();
    } else {
      pull = existing; // pending/fetching found by date (shouldn't happen — resumed above)
    }
  }
  if (!pull) return { skipped: "no_work", more: false };

  const report: Record<string, any> = parseJson(pull.report, {});
  const saveState = async (fields: Record<string, any>) => {
    const cols = Object.keys(fields);
    await db
      .prepare(
        `UPDATE qa_disposition_pulls SET ${cols.map((c) => `${c} = ?`).join(", ")}, updated_at = ? WHERE id = ?`
      )
      .bind(...cols.map((c) => fields[c]), nowIso, pull.id)
      .run();
  };
  const markError = async (message: string) => {
    await saveState({
      status: "error",
      report: JSON.stringify({ ...report, error: message.slice(0, 300) }),
    });
    return { pull_date: pull.pull_date, status: "error", error: message.slice(0, 200), more: false };
  };

  // days_ago relative to today-local — normally 1; a pull retried across
  // midnight still exports ITS day, not a shifted one.
  const todayLocal = localDay(tz, now);
  const daysAgo = Math.round(
    (Date.parse(todayLocal) - Date.parse(pull.pull_date)) / 86_400_000
  );
  if (daysAgo < 1 || daysAgo > 20)
    return await markError(`pull_date ${pull.pull_date} out of export range (days_ago=${daysAgo})`);

  const initiate = () =>
    initiateStatsExport(
      env.DIALPAD_API_KEY!,
      {
        exportType: "records",
        statType: "dispositions",
        timezone: tz,
        targetId: callcenterId,
        daysAgo: [daysAgo, daysAgo],
      },
      fetchImpl
    );

  const phase: string = pull.phase ?? (pull.request_id ? "poll" : "initiate");
  const base = { pull_date: pull.pull_date, status: "fetching" };

  // ── initiate ────────────────────────────────────────────────────────────
  if (phase === "initiate") {
    let requestId: string;
    try {
      requestId = await initiate();
    } catch (err) {
      return await markError(String((err as any)?.message ?? err));
    }
    await saveState({ status: "fetching", request_id: requestId, phase: "poll" });
    return { ...base, phase: "poll", note: "export initiated — next step polls", more: true };
  }

  // ── poll (ONE status check; the ticker spaces the retries) ───────────────
  if (phase === "poll") {
    let csv: string | null;
    try {
      csv = await pollOnce(env.DIALPAD_API_KEY!, String(pull.request_id), fetchImpl);
    } catch (err) {
      const status = pollErrorStatus(err);
      if (status !== 400 && status !== 404) return await markError(String((err as any)?.message ?? err));
      // Dialpad expires request ids ~1 h after issue (400, later 404).
      // Results are cached by parameters, so a fresh id is usually
      // complete within a second (ShiftReport §10 probe) — re-initiate on
      // the spot, bounded.
      const reinits = (pull.reinits ?? 0) + 1;
      if (reinits > MAX_REINITS) return await markError(`export id expired ${reinits}× (last: ${String((err as any)?.message ?? err).slice(0, 80)})`);
      let requestId: string;
      try {
        requestId = await initiate();
      } catch (e2) {
        return await markError(String((e2 as any)?.message ?? e2));
      }
      await saveState({ request_id: requestId, reinits });
      return { ...base, phase: "poll", note: `export id expired — re-initiated (${reinits})`, more: true };
    }
    if (csv === null)
      return { ...base, phase: "poll", note: "export not ready — next step resumes", more: true };
    const rows = parseExportCsv(csv);
    report.rows_in_export = rows.length;
    report.with_disposition = rows.filter((r) => r.disposition_category).length;
    report.fill_updated = 0;
    report.evals_backfilled = 0;
    await saveState({
      export_json: JSON.stringify(rows),
      phase: "fill",
      cursor: 0,
      report: JSON.stringify(report),
    });
    return { ...base, phase: "fill", rows_in_export: rows.length, more: true };
  }

  // ── fill (§5.2, chunked; BEFORE any enqueue — the trigger freezes
  //    grounding + SOP retrieval into the payload at enqueue time) ─────────
  if (phase === "fill") {
    const rows: StatsRow[] = parseJson(pull.export_json, []);
    const dispositioned = rows.filter((r) => r.disposition_category);
    const cursor = Number(pull.cursor ?? 0);
    const chunk = dispositioned.slice(cursor, cursor + FILL_ROWS_PER_STEP);
    if (chunk.length) {
      const f = await fillDispositions(db, teamId, chunk);
      report.fill_updated = (report.fill_updated ?? 0) + f.fill_updated;
      report.evals_backfilled = (report.evals_backfilled ?? 0) + f.evals_backfilled;
    }
    const next = cursor + FILL_ROWS_PER_STEP;
    const done = next >= dispositioned.length;
    if (done) report.fill_missing = (report.with_disposition ?? 0) - (report.fill_updated ?? 0);
    await saveState({
      cursor: done ? 0 : next,
      phase: done ? "select" : "fill",
      report: JSON.stringify(report),
    });
    return {
      ...base,
      phase: done ? "select" : "fill",
      filled: Math.min(next, dispositioned.length),
      of: dispositioned.length,
      more: true,
    };
  }

  // ── select (§4 eligibility + one-attempt dedupe + random picks) ──────────
  if (phase === "select") {
    const rows: StatsRow[] = parseJson(pull.export_json, []);
    const roster = await db
      .prepare("SELECT id, name, email FROM qa_agents WHERE team_id = ? AND active = 1")
      .bind(teamId)
      .all<any>();
    const byEmail = new Map<string, any>(
      roster.results.filter((a: any) => a.email).map((a: any) => [String(a.email).toLowerCase(), a])
    );
    const minS = sw.min_duration_s ?? 240;
    const maxS = sw.max_duration_s ?? 1800;
    const unmatched = new Set<string>();
    const eligibleByAgent = new Map<string, StatsRow[]>();
    for (const r of rows) {
      if (!r.operator_email) continue;
      if (!byEmail.has(r.operator_email)) {
        unmatched.add(r.operator_email);
        continue;
      }
      if (r.duration_s === null || r.duration_s < minS || r.duration_s > maxS) continue;
      if (!r.recording_url) continue; // no audio → unscorable (Spanish audio SOT)
      const list = eligibleByAgent.get(r.operator_email) ?? [];
      list.push(r);
      eligibleByAgent.set(r.operator_email, list);
    }
    report.agents_matched = eligibleByAgent.size;
    report.agents_unmatched = unmatched.size;
    report.eligible = [...eligibleByAgent.values()].reduce((n, l) => n + l.length, 0);

    // Already scored/queued (triple-key + link). Prior attempts on THIS
    // export's eligible calls consume the agent's nightly slots — a crash
    // resume never over-selects, and a midday manual score counts as one of
    // the agent's calls for the day.
    const allEligibleIds = [...eligibleByAgent.values()].flat().map((r) => r.call_id);
    const attempted = await attemptedCallIds(db, teamId, allEligibleIds);

    const rng = testOpts.rng ?? Math.random;
    const perAgent = sw.per_agent ?? 3;
    const maxEnqueues = sw.max_enqueues ?? 120;
    const picks: Pick[] = [];
    let selected = 0;
    let capDropped = 0;
    for (const [agentEmail, list] of eligibleByAgent) {
      const prior = list.filter((r) => attempted.has(r.call_id)).length;
      const pool = list.filter((r) => !attempted.has(r.call_id));
      const chosen = pickForAgent(pool, Math.max(0, perAgent - prior), rng);
      selected += chosen.length;
      for (const pick of chosen) {
        if (picks.length >= maxEnqueues) {
          capDropped++;
          continue;
        }
        picks.push({
          call_id: pick.call_id,
          agent_email: agentEmail,
          disposition_category: pick.disposition_category,
          disposition: pick.disposition,
          connected_at: pick.connected_at,
          ended_at: pick.ended_at,
        });
      }
    }
    report.selected = selected;
    if (capDropped) report.cap_dropped = capDropped; // no silent caps
    report.enqueued = 0;
    report.skipped_existing = 0;
    report.errors = 0;
    await saveState({
      picks: JSON.stringify(picks),
      export_json: null, // ~100 KB/day — not kept past this phase
      phase: "enqueue",
      cursor: 0,
      report: JSON.stringify(report),
    });
    return { ...base, phase: "enqueue", selected, to_enqueue: picks.length, more: true };
  }

  // ── enqueue (3 per step through the normal scoring trigger) ─────────────
  if (phase === "enqueue") {
    const picks: Pick[] = parseJson(pull.picks, []);
    const cursor = Number(pull.cursor ?? 0);
    const chunk = picks.slice(cursor, cursor + ENQUEUES_PER_STEP);
    const reviewerEmail = (sw.reviewer_email ?? "qa-system@hellolanding.com").toLowerCase();
    const { autoScoreTrigger } = await import("../routes/scoring.js");
    const errorSamples: string[] = report.error_samples ?? [];
    for (const pick of chunk) {
      const statsContext: StatsContext = {
        disposition_category: pick.disposition_category,
        disposition: pick.disposition,
        connected_at: pick.connected_at,
        ended_at: pick.ended_at,
      };
      try {
        const res = await autoScoreTrigger(request, db, teamId, env as any, {
          callId: pick.call_id,
          agentEmail: pick.agent_email,
          managerEmail: reviewerEmail,
          suppressEmail: sw.suppress_email !== false,
          statsContext,
        });
        if (res.status === 200) report.enqueued = (report.enqueued ?? 0) + 1;
        else if (res.status === 409) report.skipped_existing = (report.skipped_existing ?? 0) + 1;
        else {
          report.errors = (report.errors ?? 0) + 1;
          if (errorSamples.length < 3)
            errorSamples.push(`${pick.call_id}: HTTP ${res.status} ${(await res.text()).slice(0, 120)}`);
        }
      } catch (err) {
        report.errors = (report.errors ?? 0) + 1;
        if (errorSamples.length < 3)
          errorSamples.push(`${pick.call_id}: ${String((err as any)?.message ?? err).slice(0, 120)}`);
      }
    }
    if (errorSamples.length) report.error_samples = errorSamples;
    const next = cursor + chunk.length;
    if (next >= picks.length) {
      await saveState({
        status: "completed",
        phase: "done",
        cursor: next,
        picks: null,
        report: JSON.stringify(report),
      });
      return { pull_date: pull.pull_date, status: "completed", phase: "done", more: false, ...report };
    }
    await saveState({ cursor: next, report: JSON.stringify(report) });
    return {
      ...base,
      phase: "enqueue",
      enqueued_so_far: next,
      of: picks.length,
      more: true,
    };
  }

  return await markError(`unknown phase ${phase}`);
}

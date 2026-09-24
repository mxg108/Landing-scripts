// Supervisor Deliverables — the machine side of every deliverable
// (SupervisorDeliverables.md §4). The Member Support Direction index says
// WHAT a supervisor must bring (abandon rate, attendance, tickets, Slack
// tags; weekly abandon / productivity / QA / CSAT; monthly; the Tuesday
// tracker). This module computes every number that a system already
// knows, so the supervisor only types the parts only they know: what
// happened, why, what was done, what is next.
//
// Sources (all already flowing into this app):
//   qa_eod_reports.report   → per-day / per-shift call metrics (ShiftReport §10)
//   qa_evaluations          → QA reviews per agent (human vs AI), scores
//   qa_coachings (+tags)    → coaching conducted, action plans, commitments
//   qa_sup_action_plans     → the §6 accountability register
//   Dialpad Stats API       → weekly productivity (per-user stats + onduty)
//                             and survey CSAT (csat records) — qa_sup_pulls
//   Snowflake (gateway)     → Mission Control ticket counts (snowflakeMcp.ts)
//   Slack                   → unattended @member-support mentions (slack.ts)
//
// Pure functions first (tested from fixtures), D1 readers second, the
// ticker step (Dialpad pulls) last. Nothing here throws out of the pump.

import {
  csvRecords,
  fetchExports,
  localDay,
  type ExportOpts,
  type FetchLike,
} from "./dialpadStats.js";
import {
  buildCalls,
  buildDuty,
  dutyIntervals,
  overlapMinutes,
  DEFAULT_SHIFTS,
  type EodSheetConfig,
  type ShiftDef,
} from "./eodReport.js";
import { fetchTicketCounts, fetchWfmCsat, fetchWfmStatusMix, type TicketCategory, type TicketCounts } from "./snowflakeMcp.js";
import { unattendedMentions, type MentionsAudit } from "./slack.js";

export const QA_SYSTEM_EMAIL = "qa-system@hellolanding.com";
const DAY_MS = 86_400_000;
const r1 = (x: number) => Math.round(x * 10) / 10;
const r2 = (x: number) => Math.round(x * 100) / 100;
const pct = (num: number, den: number): number | null => (den > 0 ? r1((num / den) * 100) : null);
const nowIso = () => new Date().toISOString();

// ── config ─────────────────────────────────────────────────────────────────

export interface Targets {
  abandon_rate_pct: number | null;
  sl_pct: number | null;
  productivity_per_hour: number | null;
  qa_score: number | null;
  csat: number | null;
  qa_reviews_per_agent_week: number | null;
}

export const DEFAULT_TARGETS: Targets = {
  abandon_rate_pct: null,
  sl_pct: 80,
  productivity_per_hour: null,
  qa_score: null,
  csat: 4.1,
  qa_reviews_per_agent_week: 2,
};

export interface DeliverablesConfig {
  enabled: boolean;
  timezone: string;
  slack_channel: string | null;
  sheet_tab_prefix: string;
  meeting_weekday: number; // 0 = Sunday … 2 = Tuesday
  targets: Targets;
  tickets: { queue_id: number; categories: TicketCategory[] };
  slack_subteams: string[];
  attendance_channel: string | null;
  wfm_dashboard_url: string | null;
  /** Snowflake tables behind the WFM workbook (parity pull via the gateway); null = not wired */
  wfm: { status_table: string | null; csat_table: string | null };
}

export interface TeamContext {
  teamId: string;
  name: string;
  tz: string;
  cfg: DeliverablesConfig;
  eod: EodSheetConfig;
  shifts: ShiftDef[];
  callcenterId: string | null;
  spreadsheetId: string | null;
}

export function normalizeConfig(raw: any, teamTz: string): DeliverablesConfig {
  const d = raw ?? {};
  return {
    enabled: d.enabled !== false,
    timezone: d.timezone ?? teamTz,
    slack_channel: d.slack_channel ?? null,
    sheet_tab_prefix: d.sheet_tab_prefix ?? "Supervisor",
    meeting_weekday: Number.isInteger(d.meeting_weekday) ? d.meeting_weekday : 2,
    targets: { ...DEFAULT_TARGETS, ...(d.targets ?? {}) },
    tickets: {
      queue_id: Number(d.tickets?.queue_id ?? 1),
      categories: Array.isArray(d.tickets?.categories) ? d.tickets.categories : [],
    },
    slack_subteams: Array.isArray(d.slack_subteams) ? d.slack_subteams : [],
    attendance_channel: d.attendance_channel ?? null,
    wfm_dashboard_url: d.wfm_dashboard_url ?? null,
    wfm: { status_table: d.wfm?.status_table ?? null, csat_table: d.wfm?.csat_table ?? null },
  };
}

export async function loadTeamContext(db: D1Database, teamId: string): Promise<TeamContext> {
  const row = await db
    .prepare("SELECT id, name, timezone, provider_config FROM teams WHERE id = ?")
    .bind(teamId)
    .first<any>();
  if (!row) throw new Error(`unknown team ${teamId}`);
  let pc: any = {};
  try {
    pc = row.provider_config ? JSON.parse(row.provider_config) : {};
  } catch {}
  const eod: EodSheetConfig = pc.eod_sheet ?? {};
  const cfg = normalizeConfig(pc.deliverables, eod.timezone ?? row.timezone);
  return {
    teamId: row.id,
    name: row.name,
    tz: cfg.timezone,
    cfg,
    eod,
    shifts: eod.shifts?.length ? eod.shifts : DEFAULT_SHIFTS,
    callcenterId: pc.callcenter_id ? String(pc.callcenter_id) : null,
    spreadsheetId: eod.spreadsheet_id ?? null,
  };
}

export async function saveTargets(db: D1Database, teamId: string, targets: Partial<Targets>): Promise<Targets> {
  const ctx = await loadTeamContext(db, teamId);
  const merged: Targets = { ...ctx.cfg.targets };
  for (const k of Object.keys(DEFAULT_TARGETS) as (keyof Targets)[]) {
    if (!(k in targets)) continue;
    const v = targets[k];
    merged[k] = v === null || v === undefined || v === ("" as any) ? null : Number(v);
    if (merged[k] !== null && !Number.isFinite(merged[k])) merged[k] = null;
  }
  await db
    .prepare(
      "UPDATE teams SET provider_config = json_set(COALESCE(provider_config,'{}'), '$.deliverables.targets', json(?)), updated_at = ? WHERE id = ?"
    )
    .bind(JSON.stringify(merged), nowIso(), teamId)
    .run();
  return merged;
}

// ── period keys (local calendar; ISO weeks Monday→Sunday) ──────────────────

const parseIso = (iso: string): [number, number, number] => {
  const [y, m, d] = iso.split("-").map(Number);
  return [y, m, d];
};
const utcMs = (iso: string) => {
  const [y, m, d] = parseIso(iso);
  return Date.UTC(y, m - 1, d);
};
const isoOf = (ms: number) => new Date(ms).toISOString().slice(0, 10);

export function addDays(iso: string, n: number): string {
  return isoOf(utcMs(iso) + n * DAY_MS);
}
export function daysBetween(a: string, b: string): number {
  return Math.round((utcMs(b) - utcMs(a)) / DAY_MS);
}
/** 0 = Sunday … 6 = Saturday */
export function weekday(iso: string): number {
  return new Date(utcMs(iso)).getUTCDay();
}

export function isoWeekKey(iso: string): string {
  const d = new Date(utcMs(iso));
  const day = (d.getUTCDay() + 6) % 7; // Mon = 0
  d.setUTCDate(d.getUTCDate() - day + 3); // Thursday of this week decides the ISO year
  const year = d.getUTCFullYear();
  const jan4 = new Date(Date.UTC(year, 0, 4));
  const mondayW1 = jan4.getTime() - ((jan4.getUTCDay() + 6) % 7) * DAY_MS;
  const week = 1 + Math.floor((d.getTime() - mondayW1) / (7 * DAY_MS));
  return `${year}-W${String(week).padStart(2, "0")}`;
}

export function weekRange(weekKey: string): { start: string; end: string } {
  const m = weekKey.match(/^(\d{4})-W(\d{2})$/);
  if (!m) throw new Error(`bad week key ${weekKey}`);
  const year = Number(m[1]);
  const week = Number(m[2]);
  const jan4 = new Date(Date.UTC(year, 0, 4));
  const mondayW1 = jan4.getTime() - ((jan4.getUTCDay() + 6) % 7) * DAY_MS;
  const start = mondayW1 + (week - 1) * 7 * DAY_MS;
  return { start: isoOf(start), end: isoOf(start + 6 * DAY_MS) };
}

export function monthRange(monthKey: string): { start: string; end: string } {
  const m = monthKey.match(/^(\d{4})-(\d{2})$/);
  if (!m) throw new Error(`bad month key ${monthKey}`);
  const y = Number(m[1]);
  const mo = Number(m[2]);
  const start = Date.UTC(y, mo - 1, 1);
  const end = Date.UTC(y, mo, 0);
  return { start: isoOf(start), end: isoOf(end) };
}

export function todayLocal(tz: string, now: Date = new Date()): string {
  return localDay(tz, now);
}

/** Next meeting day (weekday index) on or after `fromIso`. */
export function nextWeekday(fromIso: string, wd: number): string {
  const delta = (wd - weekday(fromIso) + 7) % 7;
  return addDays(fromIso, delta);
}

export type Kind = "daily" | "weekly" | "biweekly" | "monthly";

export function periodRange(kind: Kind, key: string): { start: string; end: string; label: string } {
  if (kind === "daily") return { start: key, end: key, label: key };
  if (kind === "weekly") {
    const r = weekRange(key);
    return { ...r, label: `${key} (${r.start} → ${r.end})` };
  }
  if (kind === "monthly") {
    const r = monthRange(key);
    return { ...r, label: key };
  }
  // biweekly: the 14 days ending the day before the meeting
  const end = addDays(key, -1);
  return { start: addDays(end, -13), end, label: `14 days to ${end} (meeting ${key})` };
}

export function validKey(kind: Kind, key: string): boolean {
  if (kind === "daily" || kind === "biweekly") return /^\d{4}-\d{2}-\d{2}$/.test(key) && !Number.isNaN(utcMs(key));
  if (kind === "weekly") return /^\d{4}-W\d{2}$/.test(key);
  return /^\d{4}-\d{2}$/.test(key);
}

// ── EOD-derived call metrics ───────────────────────────────────────────────

export interface WindowParts {
  inbound: number;
  outbound?: number;
  answered: number;
  abandoned: number;
  short_abandoned: number;
  missed: number;
  sl_count: number;
  sl_pct: number | null;
  abandon_pct: number | null;
  asa_s?: number | null;
  avg_wait_abandoned_s?: number | null;
  longest_wait_abandoned_s?: number | null;
  agents_on_duty?: number;
  agents_handled?: number;
  source?: string;
  reconciliation?: string;
}

export interface EodDay {
  date: string;
  status: string;
  windows: Record<string, WindowParts> | null;
  sl_target: number | null;
  error: string | null;
}

export async function eodDays(db: D1Database, teamId: string, start: string, end: string): Promise<EodDay[]> {
  const rows = (
    await db
      .prepare(
        "SELECT report_date, status, report FROM qa_eod_reports WHERE team_id = ? AND report_date BETWEEN ? AND ? ORDER BY report_date"
      )
      .bind(teamId, start, end)
      .all<any>()
  ).results;
  return rows.map((r: any) => {
    let rep: any = null;
    try {
      rep = r.report ? JSON.parse(r.report) : null;
    } catch {}
    return {
      date: r.report_date,
      status: r.status,
      windows: rep?.summary?.windows ?? null,
      sl_target: rep?.service_level?.slTargetPct ?? null,
      error: rep?.error ?? null,
    };
  });
}

export interface AbandonRollup {
  start: string;
  end: string;
  days_expected: number;
  days_with_data: number;
  days_missing: string[];
  inbound: number;
  abandoned: number;
  answered: number;
  abandon_pct: number | null;
  sl_pct: number | null;
  target: number | null;
  gap: number | null; // abandon_pct − target (positive = over target = bad)
  met: boolean | null;
  by_day: { date: string; inbound: number; abandoned: number; abandon_pct: number | null; sl_pct: number | null; weekday: string }[];
  worst_days: { date: string; abandoned: number; abandon_pct: number | null }[];
  by_shift: Record<string, { inbound: number; abandoned: number; abandon_pct: number | null; days: number }>;
  by_weekday: Record<string, { inbound: number; abandoned: number; abandon_pct: number | null; days: number }>;
}

const WD = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

export function abandonRollup(days: EodDay[], start: string, end: string, target: number | null, shiftLabels: string[]): AbandonRollup {
  const byDate = new Map(days.map((d) => [d.date, d]));
  const byDay: AbandonRollup["by_day"] = [];
  const missing: string[] = [];
  let inbound = 0, abandoned = 0, answered = 0, slCount = 0, slDen = 0;
  const byShift: AbandonRollup["by_shift"] = {};
  const byWd: AbandonRollup["by_weekday"] = {};
  for (const l of shiftLabels) byShift[l] = { inbound: 0, abandoned: 0, abandon_pct: null, days: 0 };
  for (const w of WD) byWd[w] = { inbound: 0, abandoned: 0, abandon_pct: null, days: 0 };
  const n = daysBetween(start, end) + 1;
  for (let i = 0; i < n; i++) {
    const date = addDays(start, i);
    const d = byDate.get(date);
    const full = d?.windows?.["Full day"];
    if (!full) {
      missing.push(date);
      continue;
    }
    inbound += full.inbound ?? 0;
    abandoned += full.abandoned ?? 0;
    answered += full.answered ?? 0;
    slCount += full.sl_count ?? 0;
    slDen += (full.inbound ?? 0) - (full.short_abandoned ?? 0) - (full.missed ?? 0);
    const wd = WD[weekday(date)];
    byDay.push({ date, inbound: full.inbound ?? 0, abandoned: full.abandoned ?? 0, abandon_pct: full.abandon_pct ?? pct(full.abandoned ?? 0, full.inbound ?? 0), sl_pct: full.sl_pct ?? null, weekday: wd });
    byWd[wd].inbound += full.inbound ?? 0;
    byWd[wd].abandoned += full.abandoned ?? 0;
    byWd[wd].days++;
    for (const l of shiftLabels) {
      const s = d?.windows?.[l];
      if (!s) continue;
      byShift[l].inbound += s.inbound ?? 0;
      byShift[l].abandoned += s.abandoned ?? 0;
      byShift[l].days++;
    }
  }
  for (const k of Object.keys(byShift)) byShift[k].abandon_pct = pct(byShift[k].abandoned, byShift[k].inbound);
  for (const k of Object.keys(byWd)) byWd[k].abandon_pct = pct(byWd[k].abandoned, byWd[k].inbound);
  const abandonPct = pct(abandoned, inbound);
  const worst = [...byDay].sort((a, b) => (b.abandon_pct ?? -1) - (a.abandon_pct ?? -1)).slice(0, 3);
  return {
    start, end,
    days_expected: n,
    days_with_data: byDay.length,
    days_missing: missing,
    inbound, abandoned, answered,
    abandon_pct: abandonPct,
    sl_pct: pct(slCount, slDen),
    target,
    gap: abandonPct !== null && target !== null ? r1(abandonPct - target) : null,
    met: abandonPct !== null && target !== null ? abandonPct <= target : null,
    by_day: byDay,
    worst_days: worst.map((d) => ({ date: d.date, abandoned: d.abandoned, abandon_pct: d.abandon_pct })),
    by_shift: byShift,
    by_weekday: byWd,
  };
}

// ── roster + QA coverage + coaching ────────────────────────────────────────

export interface AgentRow {
  id: number;
  name: string;
  canonical_name: string | null;
  email: string;
  supervisor: string | null;
}

export async function agentRoster(db: D1Database, teamId: string, supervisor: string | null): Promise<AgentRow[]> {
  const sql = supervisor
    ? "SELECT id, name, canonical_name, email, supervisor_email AS supervisor FROM qa_agents WHERE team_id = ? AND active = 1 AND LOWER(COALESCE(supervisor_email,'')) = LOWER(?) ORDER BY COALESCE(canonical_name, name)"
    : "SELECT id, name, canonical_name, email, supervisor_email AS supervisor FROM qa_agents WHERE team_id = ? AND active = 1 ORDER BY COALESCE(canonical_name, name)";
  const stmt = supervisor ? db.prepare(sql).bind(teamId, supervisor) : db.prepare(sql).bind(teamId);
  return (await stmt.all<any>()).results.map((r: any) => ({ ...r, email: String(r.email ?? "").toLowerCase() }));
}

/** Supervisor labels: the roster's vocabulary ∪ the registry. */
export async function supervisorLabels(db: D1Database, teamId: string): Promise<{ label: string; email: string | null; agents: number }[]> {
  const derived = (
    await db
      .prepare(
        `SELECT supervisor_email AS label, COUNT(*) AS agents FROM qa_agents
         WHERE team_id = ? AND active = 1 AND supervisor_email IS NOT NULL AND TRIM(supervisor_email) <> ''
         GROUP BY LOWER(supervisor_email)`
      )
      .bind(teamId)
      .all<any>()
  ).results;
  const registry = (
    await db.prepare("SELECT label, email FROM qa_supervisors WHERE team_id = ? AND active = 1").bind(teamId).all<any>()
  ).results;
  const out = new Map<string, { label: string; email: string | null; agents: number }>();
  for (const d of derived) out.set(d.label.toLowerCase(), { label: d.label, email: null, agents: Number(d.agents) });
  for (const r of registry) {
    const k = r.label.toLowerCase();
    const cur = out.get(k);
    if (cur) cur.email = r.email ? String(r.email).toLowerCase() : cur.email;
    else out.set(k, { label: r.label, email: r.email ? String(r.email).toLowerCase() : null, agents: 0 });
  }
  return [...out.values()].sort((a, b) => a.label.toLowerCase().localeCompare(b.label.toLowerCase()));
}

export async function labelForEmail(db: D1Database, teamId: string, email: string): Promise<string | null> {
  if (!email) return null;
  const row = await db
    .prepare("SELECT label FROM qa_supervisors WHERE team_id = ? AND active = 1 AND LOWER(email) = LOWER(?) LIMIT 1")
    .bind(teamId, email)
    .first<any>();
  return row?.label ?? null;
}

export interface QaAgentCoverage {
  agent_id: number;
  name: string;
  email: string;
  human_reviews: number;
  ai_reviews: number;
  avg_score: number | null;
  last_review_at: string | null;
  last_opportunity: string | null;
  needs_coaching: number;
  meets_target: boolean | null;
}

export interface QaCoverage {
  target_reviews: number | null;
  target_score: number | null;
  per_agent: QaAgentCoverage[];
  totals: {
    agents: number;
    human_reviews: number;
    ai_reviews: number;
    avg_score: number | null;
    agents_meeting_target: number;
    agents_below_target: number;
  };
}

export async function qaCoverage(
  db: D1Database,
  teamId: string,
  agents: AgentRow[],
  start: string,
  end: string,
  targetReviews: number | null,
  targetScore: number | null
): Promise<QaCoverage> {
  const from = `${start}T00:00:00Z`;
  const to = `${addDays(end, 1)}T00:00:00Z`;
  const per = new Map<number, QaAgentCoverage>();
  for (const a of agents)
    per.set(a.id, {
      agent_id: a.id, name: a.canonical_name || a.name, email: a.email,
      human_reviews: 0, ai_reviews: 0, avg_score: null, last_review_at: null, last_opportunity: null,
      needs_coaching: 0, meets_target: targetReviews === null ? null : false,
    });
  if (agents.length) {
    const ids = agents.map((a) => a.id);
    for (let i = 0; i < ids.length; i += 80) {
      const chunk = ids.slice(i, i + 80);
      const rows = (
        await db
          .prepare(
            `SELECT agent_id, evaluator_email, state, overall_score, needs_coaching, opportunities,
                    COALESCE(approved_at, finalized_at, created_at) AS at
             FROM qa_evaluations
             WHERE team_id = ? AND agent_id IN (${chunk.map(() => "?").join(",")})
               AND state IN ('approved','finalized')
               AND COALESCE(approved_at, finalized_at, created_at) >= ? AND COALESCE(approved_at, finalized_at, created_at) < ?
             ORDER BY at`
          )
          .bind(teamId, ...chunk, from, to)
          .all<any>()
      ).results;
      const scores = new Map<number, number[]>();
      for (const r of rows) {
        const p = per.get(r.agent_id);
        if (!p) continue;
        const human = !!r.evaluator_email && String(r.evaluator_email).toLowerCase() !== QA_SYSTEM_EMAIL;
        if (human) {
          p.human_reviews++;
          p.last_review_at = r.at;
          if (r.opportunities) p.last_opportunity = String(r.opportunities).slice(0, 240);
        } else p.ai_reviews++;
        if (r.overall_score !== null && r.overall_score !== undefined) {
          const arr = scores.get(r.agent_id) ?? [];
          arr.push(Number(r.overall_score));
          scores.set(r.agent_id, arr);
        }
        if (r.needs_coaching === "Y") p.needs_coaching++;
      }
      for (const [id, arr] of scores) {
        const p = per.get(id)!;
        p.avg_score = arr.length ? r1(arr.reduce((a, b) => a + b, 0) / arr.length) : null;
      }
    }
  }
  const list = [...per.values()];
  let scoreSum = 0, scoreN = 0, meeting = 0, below = 0, human = 0, ai = 0;
  for (const p of list) {
    if (targetReviews !== null) {
      p.meets_target = p.human_reviews >= targetReviews;
      if (p.meets_target) meeting++;
      else below++;
    }
    human += p.human_reviews;
    ai += p.ai_reviews;
    if (p.avg_score !== null) {
      scoreSum += p.avg_score;
      scoreN++;
    }
  }
  list.sort((a, b) => a.human_reviews - b.human_reviews || a.name.localeCompare(b.name));
  return {
    target_reviews: targetReviews,
    target_score: targetScore,
    per_agent: list,
    totals: {
      agents: list.length, human_reviews: human, ai_reviews: ai,
      avg_score: scoreN ? r1(scoreSum / scoreN) : null,
      agents_meeting_target: meeting, agents_below_target: below,
    },
  };
}

export interface CoachingActivity {
  sessions: {
    id: number; agent_id: number; agent_name: string; completed_at: string | null;
    coaching_summary: string | null; action_plan: string | null; action_plan_deadline: string | null;
    outcome: string | null; tags: string[]; commitments: { commitment: string; status: string }[];
  }[];
  pending: { id: number; agent_name: string; scheduled_at: string | null; created_at: string }[];
  due_confirmations: { id: number; agent_name: string; action_plan_deadline: string; open_commitments: number }[];
  counts: { conducted: number; pending: number; due_confirmations: number; commitments_open: number };
}

export async function coachingActivity(
  db: D1Database,
  teamId: string,
  agents: AgentRow[],
  start: string,
  end: string,
  today: string
): Promise<CoachingActivity> {
  const empty: CoachingActivity = { sessions: [], pending: [], due_confirmations: [], counts: { conducted: 0, pending: 0, due_confirmations: 0, commitments_open: 0 } };
  if (!agents.length) return empty;
  const ids = agents.map((a) => a.id);
  const nameOf = new Map(agents.map((a) => [a.id, a.canonical_name || a.name]));
  const from = `${start}T00:00:00Z`;
  const to = `${addDays(end, 1)}T00:00:00Z`;
  const inList = ids.slice(0, 200).map(() => "?").join(",");
  const rows = (
    await db
      .prepare(
        `SELECT id, agent_id, status, completed_at, scheduled_at, created_at, coaching_summary, action_plan,
                action_plan_deadline, outcome
         FROM qa_coachings
         WHERE team_id = ? AND agent_id IN (${inList}) AND status <> 'cancelled'
           AND ((completed_at >= ? AND completed_at < ?) OR status = 'pending'
                OR (status = 'completed' AND outcome IS NULL AND action_plan_deadline IS NOT NULL AND action_plan_deadline <= ?))
         ORDER BY COALESCE(completed_at, created_at) DESC`
      )
      .bind(teamId, ...ids.slice(0, 200), from, to, today)
      .all<any>()
  ).results;
  const sessionIds = rows.map((r: any) => r.id);
  const commits = new Map<number, { commitment: string; status: string }[]>();
  const tags = new Map<number, string[]>();
  if (sessionIds.length) {
    const ph = sessionIds.map(() => "?").join(",");
    const cRows = (
      await db
        .prepare(`SELECT coaching_id, commitment, status FROM qa_coaching_commitments WHERE coaching_id IN (${ph}) ORDER BY id`)
        .bind(...sessionIds)
        .all<any>()
    ).results;
    for (const c of cRows) {
      const arr = commits.get(c.coaching_id) ?? [];
      arr.push({ commitment: c.commitment, status: c.status });
      commits.set(c.coaching_id, arr);
    }
    try {
      const tRows = (
        await db
          .prepare(
            `SELECT l.coaching_id, t.name FROM qa_coaching_tag_links l JOIN qa_coach_tags t ON t.id = l.tag_id WHERE l.coaching_id IN (${ph})`
          )
          .bind(...sessionIds)
          .all<any>()
      ).results;
      for (const t of tRows) {
        const arr = tags.get(t.coaching_id) ?? [];
        arr.push(t.name);
        tags.set(t.coaching_id, arr);
      }
    } catch {}
  }
  const out: CoachingActivity = { ...empty, sessions: [], pending: [], due_confirmations: [] };
  let openCommits = 0;
  for (const r of rows) {
    const agentName = nameOf.get(r.agent_id) ?? "?";
    const cm = commits.get(r.id) ?? [];
    if (r.status === "pending") {
      out.pending.push({ id: r.id, agent_name: agentName, scheduled_at: r.scheduled_at, created_at: r.created_at });
      continue;
    }
    const inWindow = r.completed_at && r.completed_at >= from && r.completed_at < to;
    if (inWindow) {
      out.sessions.push({
        id: r.id, agent_id: r.agent_id, agent_name: agentName, completed_at: r.completed_at,
        coaching_summary: r.coaching_summary, action_plan: r.action_plan, action_plan_deadline: r.action_plan_deadline,
        outcome: r.outcome, tags: tags.get(r.id) ?? [], commitments: cm,
      });
      openCommits += cm.filter((c) => c.status === "open").length;
    }
    if (r.status === "completed" && !r.outcome && r.action_plan_deadline && r.action_plan_deadline <= today)
      out.due_confirmations.push({ id: r.id, agent_name: agentName, action_plan_deadline: r.action_plan_deadline, open_commitments: cm.filter((c) => c.status === "open").length });
  }
  out.counts = {
    conducted: out.sessions.length,
    pending: out.pending.length,
    due_confirmations: out.due_confirmations.length,
    commitments_open: openCommits,
  };
  return out;
}

// ── action plans (§6) ──────────────────────────────────────────────────────

export const PLAN_AREAS = ["abandon_rate", "productivity", "qa", "csat", "schedule", "tickets", "slack", "training", "attendance", "other"] as const;
export const PLAN_STATUSES = ["open", "in_progress", "done", "dropped"] as const;

export async function listActionPlans(
  db: D1Database,
  teamId: string,
  opts: { status?: "open" | "closed" | "all"; supervisor?: string | null; limit?: number } = {}
): Promise<any[]> {
  const status = opts.status ?? "open";
  const where = ["team_id = ?"];
  const binds: any[] = [teamId];
  if (status === "open") where.push("status IN ('open','in_progress')");
  else if (status === "closed") where.push("status IN ('done','dropped')");
  if (opts.supervisor) {
    where.push("(LOWER(owner) = LOWER(?) OR agent_id IN (SELECT id FROM qa_agents WHERE team_id = ? AND LOWER(COALESCE(supervisor_email,'')) = LOWER(?)))");
    binds.push(opts.supervisor, teamId, opts.supervisor);
  }
  const limit = Math.min(Math.max(opts.limit ?? 200, 1), 500);
  return (
    await db
      .prepare(
        `SELECT * FROM qa_sup_action_plans WHERE ${where.join(" AND ")}
         ORDER BY CASE status WHEN 'open' THEN 0 WHEN 'in_progress' THEN 1 WHEN 'done' THEN 2 ELSE 3 END,
                  COALESCE(follow_up_date, '9999') , id DESC LIMIT ${limit}`
      )
      .bind(...binds)
      .all<any>()
  ).results;
}

// ── Dialpad weekly pulls: productivity + CSAT (qa_sup_pulls) ───────────────

export interface AgentProductivity {
  email: string;
  name: string;
  on_roster: boolean;
  supervisor: string | null;
  answered: number;
  inbound: number;
  outbound: number;
  all_calls: number;
  missed: number;
  talk_min: number;
  on_duty_min: number;
  per_hour: number | null; // (answered + outbound) per on-duty hour
  days_on_duty: number;
}

export interface ProductivityData {
  window: { start: string; end: string };
  definition: string;
  agents: AgentProductivity[];
  team: { answered: number; outbound: number; on_duty_min: number; per_hour: number | null; agents: number };
}

const USER_COLS = ["all_calls", "inbound_calls", "outbound_calls", "answered", "missed", "talk_duration"] as const;

export function computeProductivity(
  userRows: Record<string, string>[],
  dutyRows: Record<string, string>[],
  start: string,
  end: string,
  roster: AgentRow[]
): ProductivityData {
  const byEmail = new Map<string, Record<string, number> & { name: string; days: Set<string> }>();
  for (const u of userRows) {
    const date = (u.date ?? "").slice(0, 10);
    if (!date || date < start || date > end) continue;
    if ((u.type || "user") !== "user" || !u.email) continue;
    const email = u.email.toLowerCase();
    let cur = byEmail.get(email);
    if (!cur) {
      cur = Object.assign({ name: u.name ?? "", days: new Set<string>() }, Object.fromEntries(USER_COLS.map((c) => [c, 0]))) as any;
      byEmail.set(email, cur!);
    }
    for (const c of USER_COLS) cur![c] = r2((cur![c] ?? 0) + Number(u[c] || 0));
    if (Number(u.all_calls || 0) > 0) cur!.days.add(date);
    if (!cur!.name && u.name) cur!.name = u.name;
  }
  const duty = buildDuty(dutyRows);
  const winStart = utcMs(start);
  const winEnd = utcMs(end) + DAY_MS;
  const intervals = dutyIntervals(duty, winEnd);
  const dutyNames = new Map<string, string>();
  for (const d of duty) dutyNames.set(d.email, d.name);
  const rosterByEmail = new Map(roster.map((a) => [a.email, a]));
  const emails = new Set<string>([...byEmail.keys(), ...rosterByEmail.keys()]);
  for (const [email, ivs] of intervals) if (overlapMinutes(ivs, winStart, winEnd) > 0) emails.add(email);
  const agents: AgentProductivity[] = [];
  let tAnswered = 0, tOutbound = 0, tDuty = 0;
  for (const email of [...emails].sort()) {
    const u = byEmail.get(email);
    const ros = rosterByEmail.get(email);
    const ivs = intervals.get(email) ?? [];
    const onMin = Math.round(overlapMinutes(ivs, winStart, winEnd));
    const answered = u?.answered ?? 0;
    const outbound = u?.outbound_calls ?? 0;
    const perHour = onMin >= 60 ? r2((answered + outbound) / (onMin / 60)) : null;
    const dutyDays = new Set<string>();
    for (const [a, b] of ivs) {
      const s = Math.max(a, winStart), e = Math.min(b, winEnd);
      for (let t = s; t < e; t += DAY_MS) dutyDays.add(isoOf(t));
    }
    if (!u && onMin === 0 && !ros) continue;
    agents.push({
      email,
      name: ros?.canonical_name || ros?.name || u?.name || dutyNames.get(email) || email,
      on_roster: !!ros,
      supervisor: ros?.supervisor ?? null,
      answered, inbound: u?.inbound_calls ?? 0, outbound, all_calls: u?.all_calls ?? 0, missed: u?.missed ?? 0,
      talk_min: r1(u?.talk_duration ?? 0), on_duty_min: onMin, per_hour: perHour,
      days_on_duty: Math.max(dutyDays.size, u?.days.size ?? 0),
    });
    if (ros || u) {
      tAnswered += answered;
      tOutbound += outbound;
      tDuty += onMin;
    }
  }
  agents.sort((a, b) => (b.per_hour ?? -1) - (a.per_hour ?? -1));
  return {
    window: { start, end },
    definition: "per_hour = (answered inbound + outbound calls) ÷ on-duty hours (Dialpad per-user stats + onduty records); agents with < 60 on-duty minutes have no rate",
    agents,
    team: { answered: tAnswered, outbound: tOutbound, on_duty_min: tDuty, per_hour: tDuty >= 60 ? r2((tAnswered + tOutbound) / (tDuty / 60)) : null, agents: agents.filter((a) => a.on_roster).length },
  };
}

export interface AgentCsat {
  email: string;
  name: string;
  on_roster: boolean;
  supervisor: string | null;
  responses: number;
  avg: number | null;
  low_calls: { call_id: string; score: number; link: string }[];
}

export interface CsatData {
  window: { start: string; end: string };
  columns: { call_id: string | null; score: string | null };
  responses: number;
  attributed: number;
  team_avg: number | null;
  agents: AgentCsat[];
  note?: string;
}

const pick = (row: Record<string, string>, names: string[]): string | null => {
  const keys = Object.keys(row);
  for (const n of names) {
    const k = keys.find((x) => x.toLowerCase() === n);
    if (k) return k;
  }
  return null;
};

export function computeCsat(
  csatRows: Record<string, string>[],
  callRecords: Record<string, string>[],
  start: string,
  end: string,
  roster: AgentRow[],
  target: number | null
): CsatData {
  const empty: CsatData = { window: { start, end }, columns: { call_id: null, score: null }, responses: 0, attributed: 0, team_avg: null, agents: [] };
  if (!csatRows.length) return { ...empty, note: "no survey responses in the window" };
  const callIdCol = pick(csatRows[0], ["call_id", "entry_point_call_id", "master_call_id"]);
  const scoreCol = pick(csatRows[0], ["response", "csat", "csat_score", "score", "rating", "survey_response"]);
  const emailCol = pick(csatRows[0], ["email", "user_email", "operator_email", "agent_email"]);
  // Dialpad emits AI-estimated and caller-given scores side by side (the WFM
  // dashboard's "CSAT (Human)" = AvgIf(Response, Ai Human = "human")); only
  // the caller's score counts here.
  const kindCol = pick(csatRows[0], ["ai_human", "ai_or_human", "type", "source", "csat_type", "response_type"]);
  if (!callIdCol || !scoreCol)
    return { ...empty, columns: { call_id: callIdCol, score: scoreCol }, responses: csatRows.length, note: `unrecognized csat export columns: ${Object.keys(csatRows[0]).join(", ")}` };
  if (kindCol) csatRows = csatRows.filter((r) => /human|survey|caller/i.test(r[kindCol] ?? "") || !/ai/i.test(r[kindCol] ?? ""));
  const { calls, legs } = buildCalls(callRecords, { slSeconds: 30, shortAbandonS: 6 });
  const agentByCall = new Map<string, { email: string; name: string }>();
  for (const c of calls) if (c.agent_email) agentByCall.set(c.call_id, { email: c.agent_email.toLowerCase(), name: c.agent_name });
  for (const l of legs) if (l.email) agentByCall.set(l.call_id, { email: l.email.toLowerCase(), name: l.name });
  const rosterByEmail = new Map(roster.map((a) => [a.email, a]));
  const per = new Map<string, AgentCsat & { sum: number }>();
  let responses = 0, attributed = 0, teamSum = 0;
  for (const r of csatRows) {
    const score = Number(r[scoreCol]);
    if (!Number.isFinite(score)) continue;
    responses++;
    teamSum += score;
    const cid = (r[callIdCol] ?? "").trim();
    const viaEmail = emailCol && r[emailCol] ? { email: r[emailCol].toLowerCase(), name: "" } : null;
    const who = agentByCall.get(cid) ?? viaEmail;
    if (!who) continue;
    attributed++;
    let p = per.get(who.email);
    if (!p) {
      const ros = rosterByEmail.get(who.email);
      p = { email: who.email, name: ros?.canonical_name || ros?.name || who.name || who.email, on_roster: !!ros, supervisor: ros?.supervisor ?? null, responses: 0, avg: null, low_calls: [], sum: 0 };
      per.set(who.email, p);
    }
    p.responses++;
    p.sum += score;
    if (target !== null && score < target && cid) p.low_calls.push({ call_id: cid, score, link: `https://dialpad.com/callhistory/callreview/${cid}` });
  }
  const agents = [...per.values()].map((p) => ({ ...p, avg: p.responses ? r2(p.sum / p.responses) : null, low_calls: p.low_calls.slice(0, 5), sum: undefined }));
  agents.sort((a, b) => (a.avg ?? 99) - (b.avg ?? 99));
  return {
    window: { start, end },
    columns: { call_id: callIdCol, score: scoreCol },
    responses, attributed,
    team_avg: responses ? r2(teamSum / responses) : null,
    agents: agents as AgentCsat[],
  };
}

export function pullExports(daysAgoStart: number, daysAgoEnd: number, callcenterId: string, tz: string): Record<string, ExportOpts> {
  const base = { timezone: tz, targetId: callcenterId } as const;
  const a = Math.max(1, daysAgoStart), b = Math.max(a, daysAgoEnd);
  return {
    [`users:${a}-${b}`]: { ...base, exportType: "stats", statType: "calls", daysAgo: [a, b] },
    [`onduty:${a}-${b + 1}`]: { ...base, exportType: "records", statType: "onduty", daysAgo: [a, b + 1] },
    [`csat:${a}-${b}`]: { ...base, exportType: "records", statType: "csat", daysAgo: [a, b] },
    [`calls:${a}-${b}`]: { ...base, exportType: "records", statType: "calls", daysAgo: [a, b] },
  };
}

export interface PullEnv {
  DIALPAD_API_KEY?: string;
  SNOWFLAKE_MCP_TOKEN?: string;
}
export interface PullTestOpts {
  nowMs?: number;
  fetchImpl?: FetchLike;
  pollAttempts?: number;
  pollSpacingMs?: number;
}

/** Get-or-create the shared pull for a window (clipped to yesterday). */
export async function ensurePull(db: D1Database, teamId: string, start: string, end: string, tz: string, now: Date = new Date()): Promise<any> {
  const yesterday = addDays(todayLocal(tz, now), -1);
  const clippedEnd = end > yesterday ? yesterday : end;
  if (clippedEnd < start) return { status: "future", window_start: start, window_end: clippedEnd, note: "window has no completed day yet" };
  await db
    .prepare("INSERT INTO qa_sup_pulls (team_id, window_start, window_end) VALUES (?,?,?) ON CONFLICT(team_id, window_start, window_end) DO NOTHING")
    .bind(teamId, start, clippedEnd)
    .run();
  const row = await db
    .prepare("SELECT * FROM qa_sup_pulls WHERE team_id = ? AND window_start = ? AND window_end = ?")
    .bind(teamId, start, clippedEnd)
    .first<any>();
  return row;
}

export async function resetPull(db: D1Database, id: number): Promise<void> {
  await db
    .prepare("UPDATE qa_sup_pulls SET status = 'pending', export_ids = NULL, data = NULL, attempts = 0, updated_at = ? WHERE id = ?")
    .bind(nowIso(), id)
    .run();
}

/** Ticker step: advance the oldest pending/fetching pull one bounded poll. */
export async function runSupervisorPulls(db: D1Database, env: PullEnv, testOpts: PullTestOpts = {}): Promise<Record<string, any>> {
  if (!env.DIALPAD_API_KEY) return { skipped: "no_dialpad_key" };
  const row = await db
    .prepare("SELECT * FROM qa_sup_pulls WHERE status IN ('pending','fetching') ORDER BY id LIMIT 1")
    .first<any>();
  if (!row) return { skipped: "no_work" };
  const now = new Date(testOpts.nowMs ?? Date.now());
  const fetchImpl = testOpts.fetchImpl ?? fetch;
  const at = now.toISOString();
  const fail = async (message: string) => {
    await db
      .prepare("UPDATE qa_sup_pulls SET status = 'error', data = ?, attempts = attempts + 1, updated_at = ? WHERE id = ?")
      .bind(JSON.stringify({ error: message.slice(0, 300) }), at, row.id)
      .run();
    return { id: row.id, status: "error", error: message.slice(0, 200) };
  };
  let ctx: TeamContext;
  try {
    ctx = await loadTeamContext(db, row.team_id);
  } catch (err) {
    return await fail(String((err as any)?.message ?? err));
  }
  if (!ctx.callcenterId) return await fail("team has no callcenter_id");
  const today = todayLocal(ctx.tz, now);
  const a = daysBetween(row.window_end, today);
  const b = daysBetween(row.window_start, today);
  if (a < 1) return await fail("window not complete yet");
  if (b > 60) return await fail("window too old for the Stats API (> 60 days)");
  const needed = pullExports(a, b, ctx.callcenterId, ctx.tz);
  let stored: Record<string, string> = {};
  try {
    stored = row.export_ids ? JSON.parse(row.export_ids) : {};
  } catch {}
  let fetched: Awaited<ReturnType<typeof fetchExports>>;
  try {
    fetched = await fetchExports(env.DIALPAD_API_KEY, needed, stored, fetchImpl, testOpts.pollAttempts, testOpts.pollSpacingMs);
  } catch (err) {
    return await fail(`export: ${String((err as any)?.message ?? err)}`);
  }
  const ids: Record<string, string> = {};
  for (const k of Object.keys(needed)) if (fetched.ids[k]) ids[k] = fetched.ids[k];
  await db
    .prepare("UPDATE qa_sup_pulls SET status = 'fetching', export_ids = ?, updated_at = ? WHERE id = ?")
    .bind(JSON.stringify(ids), at, row.id)
    .run();
  const missing = Object.keys(needed).filter((k) => !(k in fetched.csvs));
  if (missing.length) return { id: row.id, status: "fetching", more: true, note: `export not ready (${missing.join(",")})` };
  const get = (prefix: string) =>
    Object.entries(fetched.csvs).filter(([k]) => k.startsWith(prefix)).flatMap(([, t]) => csvRecords(t));
  const roster = await agentRoster(db, row.team_id, null);
  let data: any;
  try {
    data = {
      window: { start: row.window_start, end: row.window_end },
      productivity: computeProductivity(get("users:"), get("onduty:"), row.window_start, row.window_end, roster),
      csat: computeCsat(get("csat:"), get("calls:"), row.window_start, row.window_end, roster, ctx.cfg.targets.csat),
      export_ids: ids,
      reinitiated: fetched.reinitiated,
      generated_at: at,
    };
  } catch (err) {
    return await fail(`compute: ${String((err as any)?.message ?? err)}`);
  }
  // WFM parity (optional): the same numbers the Sigma workbook shows, when
  // the gateway token and the two table paths are configured. Never blocks
  // the Dialpad-based facts above.
  if (ctx.cfg.wfm.status_table || ctx.cfg.wfm.csat_table) {
    const [mix, csat] = await Promise.all([
      fetchWfmStatusMix(env.SNOWFLAKE_MCP_TOKEN, ctx.cfg.wfm.status_table, row.window_start, row.window_end, fetchImpl),
      fetchWfmCsat(env.SNOWFLAKE_MCP_TOKEN, ctx.cfg.wfm.csat_table, row.window_start, row.window_end, fetchImpl),
    ]);
    data.wfm = {
      status_mix: "error" in mix ? { status: "unavailable", error: mix.error } : { status: "ready", ...mix },
      csat: "error" in csat ? { status: "unavailable", error: csat.error } : { status: "ready", ...csat },
    };
  }
  await db
    .prepare("UPDATE qa_sup_pulls SET status = 'ready', data = ?, updated_at = ? WHERE id = ?")
    .bind(JSON.stringify(data), at, row.id)
    .run();
  return { id: row.id, status: "ready", window: data.window, agents: data.productivity.agents.length, csat_responses: data.csat.responses };
}

// ── facts per kind ─────────────────────────────────────────────────────────

export interface FactsEnv {
  SNOWFLAKE_MCP_TOKEN?: string;
  SLACK_USER_TOKEN?: string;
}

const sheetUrl = (id: string | null, tab?: string) =>
  id ? `https://docs.google.com/spreadsheets/d/${id}/edit${tab ? `#gid=0` : ""}` : null;

function scopePull(pull: any, agents: AgentRow[], supervisor: string | null, targets: Targets) {
  const status = pull?.status ?? "missing";
  if (status !== "ready" || !pull?.data) {
    let data: any = null;
    try {
      data = pull?.data ? JSON.parse(pull.data) : null;
    } catch {}
    return { productivity: { status, error: data?.error ?? null }, csat: { status, error: data?.error ?? null }, pull_id: pull?.id ?? null };
  }
  let d: any;
  try {
    d = JSON.parse(pull.data);
  } catch {
    return { productivity: { status: "error", error: "bad pull data" }, csat: { status: "error", error: "bad pull data" }, pull_id: pull.id };
  }
  const mine = new Set(agents.map((a) => a.email));
  const scope = (list: any[]) => (supervisor ? list.filter((x) => mine.has(x.email)) : list.filter((x) => x.on_roster || !supervisor));
  const prod = d.productivity as ProductivityData;
  const csat = d.csat as CsatData;
  const pAgents = scope(prod.agents);
  const t = targets.productivity_per_hour;
  const cAgents = scope(csat.agents);
  const ct = targets.csat;
  const wfm = d.wfm ?? null;
  const wfmScope = (list: any[] | undefined, key: string) =>
    (list ?? []).filter((x) => (supervisor ? mine.has(x[key]) : true));
  return {
    pull_id: pull.id,
    productivity: {
      status: "ready",
      window: prod.window,
      definition: prod.definition,
      target: t,
      team: prod.team,
      agents: pAgents,
      above_target: t === null ? [] : pAgents.filter((a) => a.per_hour !== null && a.per_hour >= t).map((a) => a.name),
      below_target: t === null ? [] : pAgents.filter((a) => a.per_hour !== null && a.per_hour < t).map((a) => a.name),
      wfm: wfm?.status_mix ? { ...wfm.status_mix, agents: wfmScope(wfm.status_mix.agents, "email") } : null,
      generated_at: d.generated_at,
    },
    csat: {
      status: "ready",
      window: csat.window,
      target: ct,
      responses: csat.responses,
      attributed: csat.attributed,
      team_avg: csat.team_avg,
      agents: cAgents,
      below_target: ct === null ? [] : cAgents.filter((a) => a.avg !== null && a.avg < ct),
      lowest: cAgents.filter((a) => a.avg !== null).slice(0, 3),
      note: csat.note ?? null,
      wfm: wfm?.csat ? { ...wfm.csat, agents: wfmScope(wfm.csat.agents, "operator") } : null,
      generated_at: d.generated_at,
    },
  };
}

export async function buildDailyFacts(db: D1Database, ctx: TeamContext, dateIso: string, shiftKey: string | null, today: string): Promise<any> {
  const [day] = await eodDays(db, ctx.teamId, dateIso, dateIso);
  const shift = shiftKey ? ctx.shifts.find((s) => s.key === shiftKey) ?? null : null;
  const windows = day?.windows ?? null;
  const full = windows?.["Full day"] ?? null;
  const shiftParts = shift && windows ? windows[shift.label] ?? null : null;
  const mtdRange = { start: `${dateIso.slice(0, 7)}-01`, end: dateIso };
  const mtd = abandonRollup(await eodDays(db, ctx.teamId, mtdRange.start, mtdRange.end), mtdRange.start, mtdRange.end, ctx.cfg.targets.abandon_rate_pct, ctx.shifts.map((s) => s.label));
  const agents = await agentRoster(db, ctx.teamId, null);
  const qa = await qaCoverage(db, ctx.teamId, agents, dateIso, dateIso, null, ctx.cfg.targets.qa_score);
  const coaching = await coachingActivity(db, ctx.teamId, agents, dateIso, dateIso, today);
  const plans = await listActionPlans(db, ctx.teamId, { status: "open", limit: 50 });
  return {
    period: { date: dateIso, shift: shift ? { key: shift.key, label: shift.label, window: `${shift.start}–${shift.end}` } : null, timezone: ctx.tz },
    calls: {
      status: day ? day.status : "missing",
      error: day?.error ?? null,
      note: !day
        ? `no EOD report row for ${dateIso} yet (the hourly job writes it at ~07:07 ${ctx.tz} the next morning)`
        : day.status !== "completed"
          ? `EOD report ${day.status}${day.error ? `: ${day.error}` : ""} — numbers may be partial`
          : null,
      target: ctx.cfg.targets.abandon_rate_pct,
      sl_target: day?.sl_target ?? ctx.cfg.targets.sl_pct,
      shift: shiftParts,
      full_day: full,
      by_shift: windows ? Object.fromEntries(ctx.shifts.map((s) => [s.label, windows[s.label] ?? null])) : null,
      mtd: { abandon_pct: mtd.abandon_pct, abandoned: mtd.abandoned, inbound: mtd.inbound, days_with_data: mtd.days_with_data, days_missing: mtd.days_missing.length },
      sheet_url: sheetUrl(ctx.spreadsheetId),
    },
    attendance: {
      agents_on_duty: shiftParts?.agents_on_duty ?? full?.agents_on_duty ?? null,
      agents_handled: shiftParts?.agents_handled ?? full?.agents_handled ?? null,
      roster_active: agents.length,
      attendance_channel: ctx.cfg.attendance_channel,
      note: "absences, late arrivals and coverage gaps are entered by the supervisor (the attendance form posts to the attendance channel; per-agent shift assignments are not on the roster yet)",
    },
    tickets: { status: "not_fetched", queue_id: ctx.cfg.tickets.queue_id, categories: ctx.cfg.tickets.categories.map((c) => c.label) },
    slack: { status: "not_fetched", subteams: ctx.cfg.slack_subteams },
    qa_today: { human_reviews: qa.totals.human_reviews, ai_reviews: qa.totals.ai_reviews, coachings_conducted: coaching.counts.conducted },
    follow_ups: { open_action_plans: plans.slice(0, 12), open_count: plans.length },
    generated_at: nowIso(),
  };
}

/** Live sections of the daily report (tickets + Slack) — fetched on demand,
 *  persisted into `auto` so the submitted snapshot carries them. */
export async function fetchDailyLive(ctx: TeamContext, dateIso: string, shiftKey: string | null, env: FactsEnv, fetchImpl: FetchLike = fetch): Promise<{ tickets: any; slack: any }> {
  const tickets: TicketCounts | { error: string } = await fetchTicketCounts(env.SNOWFLAKE_MCP_TOKEN, ctx.cfg.tickets.queue_id, ctx.cfg.tickets.categories, fetchImpl);
  const shift = shiftKey ? ctx.shifts.find((s) => s.key === shiftKey) ?? null : null;
  // Window in real UTC ms: local shift bounds (or the local day) minus the tz offset.
  const { tzOffsetMs } = await import("./dialpadStats.js");
  const localStart = shift ? utcMs(dateIso) + hm(shift.start) : utcMs(dateIso);
  let localEnd = shift ? utcMs(dateIso) + hm(shift.end) : utcMs(dateIso) + DAY_MS;
  if (localEnd <= localStart) localEnd += DAY_MS;
  const fromMs = localStart - tzOffsetMs(ctx.tz, localStart);
  const toMs = localEnd - tzOffsetMs(ctx.tz, localEnd);
  const slack: MentionsAudit | { error: string } = await unattendedMentions(env.SLACK_USER_TOKEN, ctx.cfg.slack_subteams, fromMs, toMs, { excludeChannel: ctx.cfg.slack_channel ?? undefined, fetchImpl });
  const at = nowIso();
  return {
    tickets: "error" in tickets ? { status: "unavailable", error: tickets.error, fetched_at: at } : { status: "ready", fetched_at: at, ...tickets },
    slack: "error" in slack ? { status: "unavailable", error: slack.error, fetched_at: at } : { status: "ready", fetched_at: at, ...slack },
  };
}
const hm = (s: string): number => {
  const [h, m] = s.split(":").map(Number);
  return (h * 60 + m) * 60_000;
};

export async function buildWeeklyFacts(db: D1Database, ctx: TeamContext, weekKey: string, supervisor: string | null, today: string, now: Date = new Date()): Promise<any> {
  const { start, end } = weekRange(weekKey);
  const labels = ctx.shifts.map((s) => s.label);
  const t = ctx.cfg.targets;
  const week = abandonRollup(await eodDays(db, ctx.teamId, start, end), start, end, t.abandon_rate_pct, labels);
  const mStart = `${end.slice(0, 7)}-01`;
  const mEnd = end < today ? end : today;
  const mtd = abandonRollup(await eodDays(db, ctx.teamId, mStart, mEnd), mStart, mEnd, t.abandon_rate_pct, labels);
  const prevRange = weekRange(isoWeekKey(addDays(start, -7)));
  const prev = abandonRollup(await eodDays(db, ctx.teamId, prevRange.start, prevRange.end), prevRange.start, prevRange.end, t.abandon_rate_pct, labels);
  const agents = await agentRoster(db, ctx.teamId, supervisor);
  const qa = await qaCoverage(db, ctx.teamId, agents, start, end, t.qa_reviews_per_agent_week, t.qa_score);
  const coaching = await coachingActivity(db, ctx.teamId, agents, start, end, today);
  const pull = await ensurePull(db, ctx.teamId, start, end, ctx.tz, now);
  const scoped = pull?.id ? scopePull(pull, agents, supervisor, t) : { productivity: { status: pull?.status ?? "missing", error: pull?.note ?? null }, csat: { status: pull?.status ?? "missing", error: pull?.note ?? null }, pull_id: null };
  const plans = await listActionPlans(db, ctx.teamId, { status: "open", supervisor, limit: 100 });
  const recentlyClosed = (await listActionPlans(db, ctx.teamId, { status: "closed", supervisor, limit: 20 })).filter((p) => p.closed_at && p.closed_at >= `${addDays(start, -7)}T00:00:00Z`);
  return {
    period: { week: weekKey, start, end, timezone: ctx.tz, supervisor, agents: agents.length },
    abandon: {
      week: { ...week, by_day: week.by_day, prev_week: { abandon_pct: prev.abandon_pct, abandoned: prev.abandoned, inbound: prev.inbound } },
      mtd,
      target: t.abandon_rate_pct,
      sl_target: t.sl_pct,
      sheet_url: sheetUrl(ctx.spreadsheetId),
    },
    productivity: { ...scoped.productivity, wfm_dashboard_url: ctx.cfg.wfm_dashboard_url, pull_id: scoped.pull_id },
    qa,
    coaching,
    csat: { ...scoped.csat, pull_id: scoped.pull_id },
    action_plans: { open: plans, recently_closed: recentlyClosed },
    generated_at: nowIso(),
  };
}

export async function buildMonthlyFacts(db: D1Database, ctx: TeamContext, monthKey: string, supervisor: string | null, today: string): Promise<any> {
  const { start, end } = monthRange(monthKey);
  const labels = ctx.shifts.map((s) => s.label);
  const t = ctx.cfg.targets;
  const through = end < today ? end : addDays(today, -1) < start ? start : addDays(today, -1);
  const month = abandonRollup(await eodDays(db, ctx.teamId, start, through), start, through, t.abandon_rate_pct, labels);
  const weeks: any[] = [];
  const seen = new Set<string>();
  for (let i = 0; i <= daysBetween(start, through); i++) {
    const wk = isoWeekKey(addDays(start, i));
    if (seen.has(wk)) continue;
    seen.add(wk);
    const r = weekRange(wk);
    const ws = r.start < start ? start : r.start;
    const we = r.end > through ? through : r.end;
    const roll = abandonRollup(month.by_day.length ? await eodDays(db, ctx.teamId, ws, we) : [], ws, we, t.abandon_rate_pct, labels);
    weeks.push({ week: wk, start: ws, end: we, abandon_pct: roll.abandon_pct, abandoned: roll.abandoned, inbound: roll.inbound, sl_pct: roll.sl_pct, met: roll.met });
  }
  const prevKey = start.slice(0, 7) === `${start.slice(0, 4)}-01` ? `${Number(start.slice(0, 4)) - 1}-12` : `${start.slice(0, 4)}-${String(Number(start.slice(5, 7)) - 1).padStart(2, "0")}`;
  const pr = monthRange(prevKey);
  const prev = abandonRollup(await eodDays(db, ctx.teamId, pr.start, pr.end), pr.start, pr.end, t.abandon_rate_pct, labels);
  const agents = await agentRoster(db, ctx.teamId, supervisor);
  const qa = await qaCoverage(db, ctx.teamId, agents, start, through, null, t.qa_score);
  const coaching = await coachingActivity(db, ctx.teamId, agents, start, through, today);
  const plans = await listActionPlans(db, ctx.teamId, { status: "all", supervisor, limit: 200 });
  return {
    period: { month: monthKey, start, end, through, timezone: ctx.tz, supervisor, agents: agents.length, is_closed: end < today },
    abandon: { month, weeks, prev_month: { month: prevKey, abandon_pct: prev.abandon_pct, abandoned: prev.abandoned, inbound: prev.inbound }, target: t.abandon_rate_pct, sheet_url: sheetUrl(ctx.spreadsheetId) },
    qa,
    coaching,
    action_plans: {
      open: plans.filter((p) => p.status === "open" || p.status === "in_progress"),
      closed_this_month: plans.filter((p) => p.closed_at && p.closed_at >= `${start}T00:00:00Z` && p.closed_at < `${addDays(end, 1)}T00:00:00Z`),
    },
    generated_at: nowIso(),
  };
}

export async function buildBiweeklyFacts(db: D1Database, ctx: TeamContext, meetingDate: string, today: string, now: Date = new Date()): Promise<any> {
  const { start, end } = periodRange("biweekly", meetingDate);
  const labels = ctx.shifts.map((s) => s.label);
  const t = ctx.cfg.targets;
  const fortnight = abandonRollup(await eodDays(db, ctx.teamId, start, end), start, end, t.abandon_rate_pct, labels);
  const lastWeekKey = isoWeekKey(addDays(meetingDate, -7));
  const weekly = await buildWeeklyFacts(db, ctx, lastWeekKey, null, today, now);
  const dailies = (
    await db
      .prepare(
        "SELECT id, period_key, shift, owner_email, status, manual FROM qa_sup_reports WHERE team_id = ? AND kind = 'daily' AND period_key BETWEEN ? AND ? ORDER BY period_key DESC, shift"
      )
      .bind(ctx.teamId, start, end)
      .all<any>()
  ).results.map((r: any) => {
    let m: any = {};
    try {
      m = JSON.parse(r.manual || "{}");
    } catch {}
    return { id: r.id, date: r.period_key, shift: r.shift, owner: r.owner_email, status: r.status, manual: m };
  });
  const sups = await supervisorLabels(db, ctx.teamId);
  const lastMeetings = (
    await db
      .prepare("SELECT period_key, manual FROM qa_sup_reports WHERE team_id = ? AND kind = 'biweekly' AND period_key < ? ORDER BY period_key DESC LIMIT 4")
      .bind(ctx.teamId, meetingDate)
      .all<any>()
  ).results.map((r: any) => {
    let m: any = {};
    try {
      m = JSON.parse(r.manual || "{}");
    } catch {}
    return { date: r.period_key, presenter: m.presenter ?? null };
  });
  const lastPresenter = lastMeetings.find((m) => m.presenter)?.presenter ?? null;
  const order = sups.map((s) => s.label);
  const suggested = order.length ? order[(Math.max(0, order.findIndex((l) => l.toLowerCase() === String(lastPresenter ?? "").toLowerCase())) + (lastPresenter ? 1 : 0)) % order.length] : null;
  const plans = await listActionPlans(db, ctx.teamId, { status: "open", limit: 100 });
  return {
    period: { meeting_date: meetingDate, start, end, timezone: ctx.tz, week: lastWeekKey },
    fortnight,
    weekly,
    dailies,
    rotation: { supervisors: order, last_presenter: lastPresenter, suggested_presenter: suggested, last_meetings: lastMeetings },
    action_plans: { open: plans },
    generated_at: nowIso(),
  };
}

// ── Tuesday tracker (the index's five tables) ──────────────────────────────

const fmtPct = (v: number | null | undefined) => (v === null || v === undefined ? "—" : `${v}%`);
const fmtNum = (v: number | null | undefined, d = 2) => (v === null || v === undefined ? "—" : String(Math.round(v * 10 ** d) / 10 ** d));
const clean = (s: any) => String(s ?? "").trim();

export function buildTracker(bi: any, manual: any): any {
  const w = bi.weekly ?? {};
  const t = w.abandon?.target ?? null;
  const m = manual ?? {};
  const n = m.perf_notes ?? {};
  const gap = (cur: number | null, target: number | null, higherIsBetter: boolean) => {
    if (cur === null || target === null) return target === null ? "target not set" : "no data";
    const d = Math.round((cur - target) * 100) / 100;
    const ok = higherIsBetter ? cur >= target : cur <= target;
    return `${ok ? "on target" : "below target"} (${d > 0 ? "+" : ""}${d})`;
  };
  const prod = w.productivity ?? {};
  const csat = w.csat ?? {};
  const qa = w.qa ?? {};
  const performance = [
    { area: "Abandon Rate", current: fmtPct(w.abandon?.week?.abandon_pct), target: fmtPct(t), gap: gap(w.abandon?.week?.abandon_pct ?? null, t, false), notes: n.abandon ?? "" },
    { area: "Productivity – Team", current: prod.status === "ready" ? `${fmtNum(prod.team?.per_hour)} calls/h` : `(${prod.status})`, target: prod.target !== null && prod.target !== undefined ? `${prod.target} calls/h` : "—", gap: prod.status === "ready" ? gap(prod.team?.per_hour ?? null, prod.target ?? null, true) : "no data", notes: n.prod_team ?? "" },
    { area: "Productivity – Individual", current: prod.status === "ready" ? `${(prod.above_target ?? []).length} above · ${(prod.below_target ?? []).length} below` : `(${prod.status})`, target: prod.target !== null && prod.target !== undefined ? `${prod.target} calls/h` : "—", gap: (prod.below_target ?? []).length ? `below: ${(prod.below_target ?? []).join(", ")}` : prod.status === "ready" ? "all on target" : "no data", notes: n.prod_ind ?? "" },
    { area: "QA", current: `${qa.totals?.human_reviews ?? 0} reviews · avg ${fmtNum(qa.totals?.avg_score, 1)}`, target: `${qa.target_reviews ?? "—"}/agent/week${qa.target_score ? ` · score ${qa.target_score}` : ""}`, gap: qa.totals ? `${qa.totals.agents_below_target} of ${qa.totals.agents} agents under the review target` : "no data", notes: n.qa ?? "" },
    { area: "CSAT", current: csat.status === "ready" ? `${fmtNum(csat.team_avg)} (${csat.responses ?? 0} responses)` : `(${csat.status})`, target: csat.target !== null && csat.target !== undefined ? String(csat.target) : "—", gap: csat.status === "ready" ? `${(csat.below_target ?? []).length} agents below ${csat.target}` : "no data", notes: n.csat ?? "" },
  ];
  const plans: any[] = bi.action_plans?.open ?? [];
  const byArea = (areas: string[]) => plans.filter((p) => areas.includes(p.area));
  const planLine = (list: any[]) => list.map((p) => `${p.agent_name ? p.agent_name + ": " : ""}${p.issue}`).join(" · ");
  const actionLine = (list: any[]) => list.map((p) => p.action).join(" · ");
  const followLine = (list: any[]) => list.map((p) => `${p.owner}${p.follow_up_date ? " · " + p.follow_up_date : ""}`).join(" · ");
  const sessions: any[] = w.coaching?.sessions ?? [];
  const co = m.coaching ?? {};
  const ov = (o: any, k: string, auto: string) => (o && clean(o[k]) ? clean(o[k]) : auto);
  const coachingRow = {
    area: "QA / Coaching",
    agent: ov(co.qa, "agent", sessions.map((s) => s.agent_name).join(", ") || planLine(byArea(["qa"]))),
    issue: ov(co.qa, "issue", sessions.map((s) => (s.tags?.length ? s.tags.join("/") : (s.coaching_summary ?? "").slice(0, 60))).filter(Boolean).join(" · ") || planLine(byArea(["qa"]))),
    action: ov(co.qa, "action", sessions.map((s) => s.action_plan).filter(Boolean).join(" · ") || actionLine(byArea(["qa"]))),
    follow_up: ov(co.qa, "follow_up", sessions.map((s) => s.action_plan_deadline).filter(Boolean).join(", ") || followLine(byArea(["qa"]))),
  };
  const coaching_actions = [
    { area: "Productivity", agent: ov(co.productivity, "agent", (prod.below_target ?? []).join(", ")), issue: ov(co.productivity, "issue", planLine(byArea(["productivity"]))), action: ov(co.productivity, "action", actionLine(byArea(["productivity"]))), follow_up: ov(co.productivity, "follow_up", followLine(byArea(["productivity"]))) },
    coachingRow,
    { area: "CSAT", agent: ov(co.csat, "agent", (csat.below_target ?? []).map((a: any) => `${a.name} (${a.avg})`).join(", ")), issue: ov(co.csat, "issue", planLine(byArea(["csat"]))), action: ov(co.csat, "action", actionLine(byArea(["csat"]))), follow_up: ov(co.csat, "follow_up", followLine(byArea(["csat"]))) },
    { area: "Training / Refresher", agent: ov(co.training, "agent", ""), issue: ov(co.training, "issue", planLine(byArea(["training"])) || (m.training_topics ?? "")), action: ov(co.training, "action", actionLine(byArea(["training"]))), follow_up: ov(co.training, "follow_up", followLine(byArea(["training"]))) },
  ];
  const dailies: any[] = bi.dailies ?? [];
  const join = (key: string) => dailies.map((d) => d.manual?.[key]).filter((v) => v && String(v).trim()).map((v) => String(v).trim()).slice(0, 6).join(" · ");
  const op = m.operational ?? {};
  const f = bi.fortnight ?? {};
  const worst = (f.worst_days ?? []).map((d: any) => `${d.date} ${d.abandon_pct}%`).join(", ");
  const operational = [
    { area: "Call Volume / Abandon Rate", finding: op.calls?.finding || `${fmtPct(f.abandon_pct)} over ${f.days_with_data ?? 0} days (${f.abandoned ?? 0} of ${f.inbound ?? 0}); worst: ${worst || "—"}`, impact: op.calls?.impact || join("target_impact"), action: op.calls?.action || actionLine(byArea(["abandon_rate"])), follow_up: op.calls?.follow_up || followLine(byArea(["abandon_rate"])) },
    { area: "Schedule / Coverage", finding: op.schedule?.finding || join("coverage_issues"), impact: op.schedule?.impact || join("staffing_impact"), action: op.schedule?.action || actionLine(byArea(["schedule", "attendance"])), follow_up: op.schedule?.follow_up || followLine(byArea(["schedule", "attendance"])) },
    { area: "Tickets / Backlog", finding: op.tickets?.finding || join("ticket_notes"), impact: op.tickets?.impact || "", action: op.tickets?.action || actionLine(byArea(["tickets"])), follow_up: op.tickets?.follow_up || followLine(byArea(["tickets"])) },
    { area: "Slack Tags / Escalations", finding: op.slack?.finding || join("aged_tags"), impact: op.slack?.impact || join("recurring"), action: op.slack?.action || actionLine(byArea(["slack"])), follow_up: op.slack?.follow_up || followLine(byArea(["slack"])) },
  ];
  const decisions = (Array.isArray(m.decisions) && m.decisions.length ? m.decisions : plans.slice(0, 4).map((p) => ({ topic: `${p.area}: ${p.issue}`, action: p.action, owner: p.owner, due: p.follow_up_date ?? "" }))).slice(0, 8);
  const sc = m.schedule ?? {};
  const shiftLine = Object.entries(f.by_shift ?? {}).map(([k, v]: any) => `${k} ${fmtPct(v.abandon_pct)}`).join(" · ");
  const wdLine = Object.entries(f.by_weekday ?? {}).filter(([, v]: any) => v.days).map(([k, v]: any) => `${k} ${fmtPct(v.abandon_pct)}`).join(" · ");
  const schedule = [
    { area: "Coverage", current: sc.coverage?.current ?? "", proposed: sc.coverage?.proposed ?? "", data: sc.coverage?.data || `by shift: ${shiftLine || "—"}`, impact: sc.coverage?.impact ?? "" },
    { area: "Lunch / Breaks", current: sc.lunch?.current ?? "", proposed: sc.lunch?.proposed ?? "", data: sc.lunch?.data ?? "", impact: sc.lunch?.impact ?? "" },
    { area: "Back Office", current: sc.backoffice?.current ?? "", proposed: sc.backoffice?.proposed ?? "", data: sc.backoffice?.data ?? "", impact: sc.backoffice?.impact ?? "" },
    { area: "Peak Hours", current: sc.peak?.current ?? "", proposed: sc.peak?.proposed ?? "", data: sc.peak?.data || `by weekday: ${wdLine || "—"}`, impact: sc.peak?.impact ?? "" },
  ];
  return {
    meeting_date: bi.period?.meeting_date,
    presenter: m.presenter ?? bi.rotation?.suggested_presenter ?? null,
    performance, coaching_actions, operational, decisions, schedule,
  };
}

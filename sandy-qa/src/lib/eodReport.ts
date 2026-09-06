// Member Support EOD report → Google Sheet, on the hourly pump
// (ShiftReport.md §10; port of qa-automation/AI-Scoring/scripts/ms_eod_report.py,
// which stays the reference implementation + manual backfill tool).
//
// One qa_eod_reports row per (team, local report date) is the re-run latch,
// the resume handle (Stats request ids keyed by selector) and the audit
// trail (aggregates only). Tick gate + catch-up window mirror the nightly
// disposition sweep. All failures degrade to a row status + report, never a
// throw out of the pump.
//
// Definitions (SR0, fixtures/dialpad_stats_headers.md — reconciled against
// Dialpad's own daily export and Dialpad Analytics):
//   call            = records row with target_kind = CallCenter (entry point)
//   answered        = category = incoming;  abandoned = category = abandoned
//   short abandoned = abandoned ∧ (date_ended − date_started) < short_abandon_s (6)
//   sl_count        = answered inbound ∧ time_to_answer(min) ≤ cc_service_level_seconds
//   sl_pct          = sl_count / (inbound − short_abandoned − missed)   ← 74 % / 67 %
//   durations are MINUTES; timestamps naive local (handled in a fixed frame).

import {
  csvRecords,
  DIALPAD_BASE,
  fetchExports,
  initiateExport,
  localDay,
  naiveToMs,
  type FetchLike,
} from "./dialpadStats.js";
import { listTabs, openSpreadsheet, upsertTab, type CellValue } from "./googleSheets.js";

// ── config ─────────────────────────────────────────────────────────────────

export interface ShiftDef {
  key: string;
  label: string;
  start: string; // "HH:MM" local
  end: string;   // "HH:MM" local; end <= start ⇒ crosses midnight
}

export interface EodSheetConfig {
  enabled?: boolean;
  spreadsheet_id?: string;
  timezone?: string;
  local_hour_utc?: number;
  catchup_hours?: number;
  short_abandon_s?: number;
  sl_seconds_fallback?: number;
  sl_target_pct_fallback?: number;
  shifts?: ShiftDef[];
}

export const DEFAULT_SHIFTS: ShiftDef[] = [
  { key: "morning", label: "Morning", start: "06:00", end: "16:00" },
  { key: "afternoon", label: "Afternoon", start: "12:30", end: "22:00" },
  { key: "night", label: "Night", start: "22:00", end: "06:00" },
];
export const SL_DENOMINATOR_EXCLUDES = ["short_abandoned", "missed"] as const;
const CATCHUP_HOURS_DEFAULT = 6;
const DAY_MS = 86_400_000;
const ON_DUTY = new Set(["available", "occupied", "wrapup", "busy"]);

export const SUMMARY_TAB = "Summary";
export const CALLS_TAB = "Calls";
export const AGENTS_TAB = "Agents_Daily";

// Headers are the contract with the Python reference — keep byte-identical.
export const SUMMARY_HEADER = [
  "date", "window", "window_local", "source",
  "inbound", "outbound", "answered", "abandoned", "abandon_pct",
  "short_abandoned", "missed", "cancelled", "spam", "voicemail",
  "sl_count", "sl_pct", "sl_target", "sl_formula",
  "asa_s", "avg_wait_abandoned_s", "longest_wait_abandoned_s",
  "agents_handled", "agents_on_duty", "reconciliation", "generated_at",
];
export const CALLS_HEADER = [
  "date", "started_local", "call_id", "direction", "outcome",
  "caller_number", "line_number", "agent_name", "agent_email",
  "wait_s", "talk_min", "duration_s", "transferred", "voicemail",
  "callback_type", "short_abandoned", "within_sl", "shifts",
  "dialpad_link", "legs",
];
export const AGENTS_HEADER = [
  "date", "agent_name", "agent_email", "handled_calls", "on_duty",
  "all_calls", "inbound", "outbound", "answered", "missed", "abandoned",
  "ring_no_answer", "talk_min", "hold_min", "wrapup_min",
  "on_duty_min", "first_on_duty", "last_off_duty", "shifts_on_duty",
];

// ── time helpers (naive local frame: epoch-ms AS IF UTC) ───────────────────

const r1 = (x: number) => Math.round(x * 10) / 10;
const isoDate = (ms: number) => new Date(ms).toISOString().slice(0, 10);
const hhmmss = (ms: number) => new Date(ms).toISOString().slice(11, 19);
const hhmm = (ms: number) => new Date(ms).toISOString().slice(11, 16);
const dayStartMs = (dateIso: string) => {
  const [y, m, d] = dateIso.split("-").map(Number);
  return Date.UTC(y, m - 1, d);
};
const hm = (s: string): number => {
  const [h, m] = s.split(":").map(Number);
  return (h * 60 + m) * 60_000;
};

export function dayWindow(dateIso: string): [number, number] {
  const s = dayStartMs(dateIso);
  return [s, s + DAY_MS];
}

/** [start, end) of the shift that STARTED on `dateIso`. */
export function shiftWindow(dateIso: string, shift: ShiftDef): [number, number] {
  const base = dayStartMs(dateIso);
  const start = base + hm(shift.start);
  let end = base + hm(shift.end);
  if (end <= start) end += DAY_MS;
  return [start, end];
}

export function windowLabel(shift: ShiftDef): string {
  return `${shift.start}–${shift.end}` + (hm(shift.end) <= hm(shift.start) ? " (+1)" : "");
}

const inWindow = (ms: number | null, start: number, end: number) => ms !== null && ms >= start && ms < end;

/** Shift keys containing `ms`; a shift that started the previous day is
 *  labelled `key(YYYY-MM-DD)` (02:00 → night of the day before). */
export function shiftsContaining(ms: number, shifts: ShiftDef[] = DEFAULT_SHIFTS): string[] {
  const today = isoDate(ms);
  const yesterday = isoDate(ms - DAY_MS);
  const out: string[] = [];
  for (const day of [yesterday, today]) {
    for (const sh of shifts) {
      const [s, e] = shiftWindow(day, sh);
      if (inWindow(ms, s, e)) out.push(day === today ? sh.key : `${sh.key}(${day})`);
    }
  }
  return out;
}

// ── row models ─────────────────────────────────────────────────────────────

export interface Call {
  call_id: string;
  started: number;
  direction: string;
  category: string;
  categories: Set<string>;
  external_number: string;
  internal_number: string;
  queued: number | null;
  first_rang: number | null;
  connected: number | null;
  ended: number | null;
  time_to_answer_min: number | null;
  talk_min: number | null;
  voicemail: boolean;
  callback_type: string;
  agent_name: string;
  agent_email: string;
  legs: number;
  // derived
  inbound: boolean;
  answered: boolean;
  abandoned: boolean;
  spam: boolean;
  transferred: boolean;
  short_abandoned: boolean;
  wait_s: number | null;
  duration_s: number | null;
  within_sl: boolean | null;
}

export interface AgentLeg {
  call_id: string;
  entry_point_call_id: string;
  started: number;
  connected: number | null;
  name: string;
  email: string;
}

export interface DutyRow {
  ts: number;
  email: string;
  name: string;
  status: string;
}

const num = (raw: string | undefined): number | null => {
  const s = (raw ?? "").trim();
  if (!s) return null;
  const n = Number(s);
  return Number.isFinite(n) ? n : null;
};
const cats = (raw: string | undefined) => new Set((raw ?? "").split(",").map((t) => t.trim()).filter(Boolean));

export function buildCalls(
  records: Record<string, string>[],
  opts: { slSeconds: number; shortAbandonS: number }
): { calls: Call[]; legs: AgentLeg[] } {
  const calls = new Map<string, Call>();
  const legs: AgentLeg[] = [];
  const seenLeg = new Set<string>();
  for (const r of records) {
    const started = naiveToMs(r.date_started ?? "");
    const cid = (r.call_id ?? "").trim();
    if (started === null || !cid) continue;
    const kind = (r.target_kind ?? "").trim();
    if (kind === "CallCenter") {
      if (calls.has(cid)) continue; // dedupe across merged exports
      const category = (r.category ?? "").trim();
      const direction = (r.direction ?? "").trim();
      const categories = cats(r.categories);
      const queued = naiveToMs(r.date_queued ?? "");
      const firstRang = naiveToMs(r.date_first_rang ?? "");
      const connected = naiveToMs(r.date_connected ?? "");
      const ended = naiveToMs(r.date_ended ?? "");
      const tta = num(r.time_to_answer);
      const inbound = direction === "inbound";
      const answered = category === "incoming";
      const abandoned = category === "abandoned";
      const lifetimeS = ended === null ? null : (ended - started) / 1000;
      let wait: number | null = null;
      if (answered) wait = tta === null ? null : r1(tta * 60);
      else if (ended !== null) {
        const anchors = [queued, firstRang].filter((t): t is number => t !== null);
        const anchor = anchors.length ? Math.min(...anchors) : started;
        wait = r1((ended - anchor) / 1000);
      }
      calls.set(cid, {
        call_id: cid,
        started,
        direction,
        category,
        categories,
        external_number: (r.external_number ?? "").trim(),
        internal_number: (r.internal_number ?? "").trim(),
        queued,
        first_rang: firstRang,
        connected,
        ended,
        time_to_answer_min: tta,
        talk_min: num(r.talk_duration),
        voicemail: (r.voicemail ?? "").trim().toLowerCase() === "true",
        callback_type: (r.callback_type ?? "").trim(),
        agent_name: "",
        agent_email: "",
        legs: 0,
        inbound,
        answered,
        abandoned,
        spam: categories.has("spam"),
        transferred: categories.has("transferred_to") || categories.has("transferred_out"),
        short_abandoned: abandoned && lifetimeS !== null && lifetimeS < opts.shortAbandonS,
        wait_s: wait,
        duration_s: connected !== null && ended !== null ? r1((ended - connected) / 1000) : null,
        within_sl: inbound && answered && tta !== null ? tta * 60 <= opts.slSeconds : null,
      });
    } else if (kind === "UserProfile" && (r.email ?? "").trim()) {
      if (seenLeg.has(cid)) continue;
      seenLeg.add(cid);
      legs.push({
        call_id: cid,
        entry_point_call_id: (r.entry_point_call_id ?? "").trim(),
        started,
        connected: naiveToMs(r.date_connected ?? ""),
        name: (r.name ?? "").trim(),
        email: (r.email ?? "").trim().toLowerCase(),
      });
    }
  }
  // Join: the agent who CONNECTED (earliest) wins; else the first leg.
  const byEntry = new Map<string, AgentLeg[]>();
  for (const l of legs) {
    if (!l.entry_point_call_id) continue;
    const list = byEntry.get(l.entry_point_call_id) ?? [];
    list.push(l);
    byEntry.set(l.entry_point_call_id, list);
  }
  for (const c of calls.values()) {
    const group = byEntry.get(c.call_id) ?? [];
    c.legs = group.length;
    if (!group.length) continue;
    const connected = group.filter((l) => l.connected !== null).sort((a, b) => a.connected! - b.connected!);
    const pick = connected[0] ?? [...group].sort((a, b) => a.started - b.started)[0];
    c.agent_name = pick.name;
    c.agent_email = pick.email;
  }
  return { calls: [...calls.values()], legs };
}

export function buildDuty(rows: Record<string, string>[]): DutyRow[] {
  const out: DutyRow[] = [];
  const seen = new Set<string>();
  for (const r of rows) {
    const ts = naiveToMs(r.date ?? "");
    const email = (r.email ?? "").trim().toLowerCase();
    const rid = (r.record_id ?? "").trim();
    if (ts === null || !email || (rid && seen.has(rid))) continue;
    if (rid) seen.add(rid);
    out.push({ ts, email, name: (r.name ?? "").trim(), status: (r.on_duty_status ?? "").trim().toLowerCase() });
  }
  return out;
}

/** Per agent: [start, end) stretches in an on-duty state; a trailing
 *  on-state closes at `horizon`. */
export function dutyIntervals(rows: DutyRow[], horizon: number): Map<string, [number, number][]> {
  const byEmail = new Map<string, DutyRow[]>();
  for (const r of rows) {
    const list = byEmail.get(r.email) ?? [];
    list.push(r);
    byEmail.set(r.email, list);
  }
  const out = new Map<string, [number, number][]>();
  for (const [email, seq] of byEmail) {
    seq.sort((a, b) => a.ts - b.ts);
    const ivs: [number, number][] = [];
    let open: number | null = null;
    for (const r of seq) {
      const on = ON_DUTY.has(r.status);
      if (on && open === null) open = r.ts;
      else if (!on && open !== null) {
        if (r.ts > open) ivs.push([open, r.ts]);
        open = null;
      }
    }
    if (open !== null && horizon > open) ivs.push([open, horizon]);
    out.set(email, ivs);
  }
  return out;
}

export function overlapMinutes(ivs: [number, number][], start: number, end: number): number {
  let total = 0;
  for (const [a, b] of ivs) {
    const lo = Math.max(a, start);
    const hi = Math.min(b, end);
    if (hi > lo) total += (hi - lo) / 60_000;
  }
  return r1(total);
}

const overlaps = (ivs: [number, number][], start: number, end: number) => ivs.some(([a, b]) => b > start && a < end);

// ── metrics ────────────────────────────────────────────────────────────────

export type Parts = Record<string, number | null>;

export function slPct(slCount: number, parts: Parts): number | null {
  const den = (parts.inbound ?? 0) - SL_DENOMINATOR_EXCLUDES.reduce((n, k) => n + (parts[k] ?? 0), 0);
  return den > 0 ? r1((100 * slCount) / den) : null;
}

export const slFormula = () => "sl_count / (inbound" + SL_DENOMINATOR_EXCLUDES.map((k) => ` − ${k}`).join("") + ")";

const mean = (xs: number[]) => (xs.length ? r1(xs.reduce((a, b) => a + b, 0) / xs.length) : null);

export function summarizeWindow(
  calls: Call[],
  legs: AgentLeg[],
  intervals: Map<string, [number, number][]>,
  start: number,
  end: number
): Parts {
  const win = calls.filter((c) => inWindow(c.started, start, end));
  const inbound = win.filter((c) => c.inbound);
  const abandoned = inbound.filter((c) => c.abandoned);
  const answered = inbound.filter((c) => c.answered);
  const p: Parts = {
    inbound: inbound.length,
    outbound: win.filter((c) => c.direction === "outbound").length,
    answered: answered.length,
    abandoned: abandoned.length,
    short_abandoned: abandoned.filter((c) => c.short_abandoned).length,
    missed: inbound.filter((c) => c.category === "missed").length,
    cancelled: inbound.filter((c) => c.category === "cancelled").length,
    spam: inbound.filter((c) => c.spam).length,
    voicemail: inbound.filter((c) => c.voicemail).length,
  };
  p.sl_count = answered.filter((c) => c.within_sl === true).length;
  p.sl_pct = slPct(p.sl_count, p);
  p.abandon_pct = inbound.length ? r1((100 * abandoned.length) / inbound.length) : null;
  p.asa_s = mean(answered.filter((c) => c.time_to_answer_min !== null).map((c) => c.time_to_answer_min! * 60));
  const waits = abandoned.map((c) => c.wait_s).filter((w): w is number => w !== null);
  p.avg_wait_abandoned_s = mean(waits);
  p.longest_wait_abandoned_s = waits.length ? Math.max(...waits) : null;
  p.agents_handled = new Set(legs.filter((l) => inWindow(l.started, start, end)).map((l) => l.email)).size;
  p.agents_on_duty = [...intervals.values()].filter((ivs) => overlaps(ivs, start, end)).length;
  return p;
}

/** Dialpad's daily stats row (group_by date) → the same keys. */
export function officialDay(row: Record<string, string>): Parts {
  const g = (k: string) => Math.trunc(Number(row[k] || 0)) || 0;
  const p: Parts = {
    inbound: g("inbound_calls"), outbound: g("outbound_calls"), answered: g("answered"),
    abandoned: g("abandoned"), short_abandoned: g("short_abandoned"), missed: g("missed"),
    cancelled: g("cancelled"), spam: g("spam"), voicemail: g("voicemails"), sl_count: g("service_level"),
  };
  p.sl_pct = slPct(p.sl_count!, p);
  p.abandon_pct = p.inbound ? r1((100 * p.abandoned!) / p.inbound) : null;
  const asa = num(row.asa);
  p.asa_s = asa === null ? null : r1(asa * 60);
  return p;
}

export function reconcile(official: Parts, derived: Parts): string {
  const diffs = ["inbound", "abandoned", "short_abandoned", "sl_count"]
    .filter((k) => derived[k] !== official[k])
    .map((k) => `${k} records=${derived[k]} vs dialpad=${official[k]}`);
  return diffs.length ? "CHECK: " + diffs.join("; ") : "OK";
}

// ── report assembly ────────────────────────────────────────────────────────

export interface ReportInputs {
  records: Record<string, string>[];   // calls records (both days merged)
  duty: Record<string, string>[];      // onduty records
  daily: Record<string, string>[];     // stats calls group_by date
  users: Record<string, string>[];     // stats calls per user
}

export interface ReportOpts {
  slSeconds: number;
  slTargetPct: number;
  shortAbandonS: number;
  shifts: ShiftDef[];
  generatedAt: string;
}

export interface Report {
  summaryRows: CellValue[][];
  callRows: CellValue[][];
  agentRows: CellValue[][];
  summary: Record<string, any>;
}

const yn = (v: boolean | null) => (v === null ? "" : v ? "Y" : "N");
const cell = (v: number | string | null | undefined): CellValue => (v === null || v === undefined ? "" : v);

function summaryRow(
  dateIso: string, window: string, windowLocal: string, source: string,
  p: Parts, slTarget: string, recon: string, generatedAt: string
): CellValue[] {
  return [
    dateIso, window, windowLocal, source,
    cell(p.inbound), cell(p.outbound), cell(p.answered), cell(p.abandoned), cell(p.abandon_pct),
    cell(p.short_abandoned), cell(p.missed), cell(p.cancelled), cell(p.spam), cell(p.voicemail),
    cell(p.sl_count), cell(p.sl_pct), slTarget, slFormula(),
    cell(p.asa_s), cell(p.avg_wait_abandoned_s), cell(p.longest_wait_abandoned_s),
    cell(p.agents_handled), cell(p.agents_on_duty), recon, generatedAt,
  ];
}

export function buildReport(dateIso: string, inputs: ReportInputs, opts: ReportOpts): Report {
  const { calls, legs } = buildCalls(inputs.records, { slSeconds: opts.slSeconds, shortAbandonS: opts.shortAbandonS });
  const duty = buildDuty(inputs.duty);
  const horizon = Math.max(...duty.map((d) => d.ts), ...calls.map((c) => c.started), 0) + 60_000;
  const intervals = dutyIntervals(duty, horizon);
  const slTarget = `${opts.slTargetPct}% ≤ ${opts.slSeconds}s`;
  const [dayS, dayE] = dayWindow(dateIso);

  const summaryRows: CellValue[][] = [];
  const summary: Record<string, any> = { date: dateIso, windows: {} };
  const derived = summarizeWindow(calls, legs, intervals, dayS, dayE);
  const officialRow = inputs.daily.find((r) => r.date === dateIso);
  let full: Parts;
  let source: string;
  let recon: string;
  if (officialRow) {
    full = officialDay(officialRow);
    full.agents_handled = derived.agents_handled;
    full.agents_on_duty = derived.agents_on_duty;
    full.avg_wait_abandoned_s = derived.avg_wait_abandoned_s;
    full.longest_wait_abandoned_s = derived.longest_wait_abandoned_s;
    source = "dialpad daily stats";
    recon = reconcile(full, derived);
  } else {
    full = derived;
    source = "records (no daily stats row)";
    recon = "no dialpad daily row";
  }
  summaryRows.push(summaryRow(dateIso, "Full day", "00:00–24:00", source, full, slTarget, recon, opts.generatedAt));
  summary.windows["Full day"] = { ...full, source, reconciliation: recon };
  for (const sh of opts.shifts) {
    const [s, e] = shiftWindow(dateIso, sh);
    const p = summarizeWindow(calls, legs, intervals, s, e);
    summaryRows.push(summaryRow(dateIso, sh.label, windowLabel(sh), "records", p, slTarget, "", opts.generatedAt));
    summary.windows[sh.label] = p;
  }

  const callRows: CellValue[][] = calls
    .filter((c) => isoDate(c.started) === dateIso)
    .sort((a, b) => a.started - b.started)
    .map((c) => [
      dateIso, hhmmss(c.started), c.call_id, c.direction, c.category,
      c.external_number, c.internal_number, c.agent_name, c.agent_email,
      cell(c.wait_s), cell(c.talk_min), cell(c.duration_s),
      yn(c.transferred), yn(c.voicemail), c.callback_type,
      c.abandoned ? yn(c.short_abandoned) : "", yn(c.within_sl),
      shiftsContaining(c.started, opts.shifts).join(","),
      `https://dialpad.com/callhistory/callreview/${c.call_id}`, c.legs,
    ]);

  const names = new Map<string, string>();
  for (const l of legs) names.set(l.email, l.name);
  for (const d of duty) names.set(d.email, d.name);
  const handled = new Map<string, number>();
  for (const l of legs) if (isoDate(l.started) === dateIso) handled.set(l.email, (handled.get(l.email) ?? 0) + 1);
  const users = new Map<string, Record<string, string>>();
  for (const u of inputs.users)
    if (u.date === dateIso && (u.type || "user") === "user" && u.email) users.set(u.email.toLowerCase(), u);
  const emails = new Set<string>([...handled.keys(), ...users.keys()]);
  for (const [email, ivs] of intervals) if (overlaps(ivs, dayS, dayE)) emails.add(email);
  const gi = (u: Record<string, string> | undefined, k: string): CellValue =>
    u && u[k] !== undefined && u[k] !== "" ? Math.trunc(Number(u[k])) : "";
  const gm = (u: Record<string, string> | undefined, k: string): CellValue =>
    u && u[k] !== undefined && u[k] !== "" ? Number(u[k]) : "";
  const agentRows: CellValue[][] = [...emails].sort().map((email) => {
    const u = users.get(email);
    const ivs = intervals.get(email) ?? [];
    const onMin = overlapMinutes(ivs, dayS, dayE);
    const inside = ivs.filter(([a, b]) => b > dayS && a < dayE).map(([a, b]) => [Math.max(a, dayS), Math.min(b, dayE)] as const);
    const firstOn = inside.length ? hhmm(Math.min(...inside.map((i) => i[0]))) : "";
    const lastEnd = inside.length ? Math.max(...inside.map((i) => i[1])) : null;
    const lastOff = lastEnd === null || lastEnd >= dayE ? "" : hhmm(lastEnd);
    const shiftsOn = opts.shifts.filter((sh) => overlapMinutes(ivs, ...shiftWindow(dateIso, sh)) > 0).map((sh) => sh.label);
    return [
      dateIso, u?.name || names.get(email) || "", email,
      handled.get(email) ?? 0, onMin > 0 ? "Y" : "N",
      gi(u, "all_calls"), gi(u, "inbound_calls"), gi(u, "outbound_calls"), gi(u, "answered"),
      gi(u, "missed"), gi(u, "abandoned"), gi(u, "ring_no_answer"),
      gm(u, "talk_duration"), gm(u, "hold_duration"), gm(u, "wrapup_duration"),
      onMin, firstOn, lastOff, shiftsOn.join(", "),
    ];
  });

  summary.counts = { calls: callRows.length, agents: agentRows.length };
  return { summaryRows, callRows, agentRows, summary };
}

// ── the pump job ───────────────────────────────────────────────────────────

export interface EodEnv {
  DIALPAD_API_KEY?: string;
  GSHEETS_SA_JSON?: string;
}

export interface EodTestOpts {
  nowMs?: number;
  fetchImpl?: FetchLike;
  pollAttempts?: number;
  pollSpacingMs?: number;
}

/** Selector keys → export options for report date D given today = D + daysAgo. */
export function neededExports(
  daysAgo: number,
  callcenterId: string,
  tz: string
): Record<string, Parameters<typeof initiateExport>[1]> {
  const base = { timezone: tz, targetId: callcenterId } as const;
  if (daysAgo === 1) {
    return {
      "calls:1-1": { ...base, exportType: "records", statType: "calls", daysAgo: [1, 1] },
      "calls:today": { ...base, exportType: "records", statType: "calls", isToday: true },
      "onduty:1-1": { ...base, exportType: "records", statType: "onduty", daysAgo: [1, 1] },
      "onduty:today": { ...base, exportType: "records", statType: "onduty", isToday: true },
      "daily:1-1": { ...base, exportType: "stats", statType: "calls", daysAgo: [1, 1], groupBy: "date" },
      "users:1-1": { ...base, exportType: "stats", statType: "calls", daysAgo: [1, 1] },
    };
  }
  const d = daysAgo;
  return {
    [`calls:${d - 1}-${d}`]: { ...base, exportType: "records", statType: "calls", daysAgo: [d - 1, d] },
    [`onduty:${d - 1}-${d}`]: { ...base, exportType: "records", statType: "onduty", daysAgo: [d - 1, d] },
    [`daily:${d}-${d}`]: { ...base, exportType: "stats", statType: "calls", daysAgo: [d, d], groupBy: "date" },
    [`users:${d}-${d}`]: { ...base, exportType: "stats", statType: "calls", daysAgo: [d, d] },
  };
}

export async function runEodReports(
  db: D1Database,
  env: EodEnv,
  testOpts: EodTestOpts = {}
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
    const eod: EodSheetConfig = cfg?.eod_sheet ?? {};
    if (!eod.enabled || !cfg?.callcenter_id) continue;
    try {
      out[team.id] = await eodTeam(db, env, team.id, String(cfg.callcenter_id), eod, testOpts);
    } catch (err) {
      out[team.id] = { error: String((err as any)?.message ?? err).slice(0, 200) };
    }
  }
  return out;
}

async function fetchCallCenter(
  apiKey: string,
  callcenterId: string,
  fetchImpl: FetchLike,
  fallback: { slSeconds: number; slTargetPct: number }
): Promise<{ slSeconds: number; slTargetPct: number; source: string }> {
  try {
    const res = await fetchImpl(`${DIALPAD_BASE}/callcenters/${callcenterId}`, {
      headers: { Authorization: `Bearer ${apiKey}`, Accept: "application/json" },
      signal: AbortSignal.timeout(20_000),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const alerts = ((await res.json()) as any)?.alerts ?? {};
    const secs = Number(alerts.cc_service_level_seconds);
    const pct = Number(alerts.cc_service_level);
    return {
      slSeconds: Number.isFinite(secs) && secs > 0 ? secs : fallback.slSeconds,
      slTargetPct: Number.isFinite(pct) && pct > 0 ? pct : fallback.slTargetPct,
      source: "callcenter",
    };
  } catch (err) {
    return { ...fallback, source: `fallback (${String((err as any)?.message ?? err).slice(0, 60)})` };
  }
}

async function eodTeam(
  db: D1Database,
  env: EodEnv,
  teamId: string,
  callcenterId: string,
  cfg: EodSheetConfig,
  testOpts: EodTestOpts
): Promise<any> {
  const now = new Date(testOpts.nowMs ?? Date.now());
  const fetchImpl = testOpts.fetchImpl ?? fetch;
  const tz = cfg.timezone ?? "America/Mexico_City";
  const gateHour = cfg.local_hour_utc ?? 13;
  const catchup = cfg.catchup_hours ?? CATCHUP_HOURS_DEFAULT;
  const nowIso = now.toISOString();

  // Resume any in-flight row first (slow export, worker death mid-run).
  let row = await db
    .prepare(
      "SELECT * FROM qa_eod_reports WHERE team_id = ? AND status IN ('pending','fetching') ORDER BY report_date LIMIT 1"
    )
    .bind(teamId)
    .first<any>();

  if (!row) {
    const hour = now.getUTCHours();
    if (hour < gateHour || hour >= gateHour + catchup) return { skipped: "outside_window" };
    const reportDate = localDay(tz, new Date(now.getTime() - DAY_MS));
    const existing = await db
      .prepare("SELECT * FROM qa_eod_reports WHERE team_id = ? AND report_date = ?")
      .bind(teamId, reportDate)
      .first<any>();
    if (existing?.status === "completed") return { skipped: "already_completed", report_date: reportDate };
    if (existing?.status === "error") {
      // retry inside the catch-up window with FRESH exports (same row)
      await db
        .prepare("UPDATE qa_eod_reports SET status = 'pending', export_ids = NULL, updated_at = ? WHERE id = ?")
        .bind(nowIso, existing.id)
        .run();
      row = { ...existing, status: "pending", export_ids: null };
    } else if (!existing) {
      await db
        .prepare(
          "INSERT INTO qa_eod_reports (team_id, report_date) VALUES (?, ?) ON CONFLICT(team_id, report_date) DO NOTHING"
        )
        .bind(teamId, reportDate)
        .run();
      row = await db
        .prepare("SELECT * FROM qa_eod_reports WHERE team_id = ? AND report_date = ?")
        .bind(teamId, reportDate)
        .first<any>();
    } else row = existing;
  }
  if (!row) return { skipped: "no_work" };

  const markError = async (message: string, extra: Record<string, any> = {}) => {
    await db
      .prepare("UPDATE qa_eod_reports SET status = 'error', report = ?, updated_at = ? WHERE id = ?")
      .bind(JSON.stringify({ error: message.slice(0, 300), ...extra }), nowIso, row.id)
      .run();
    return { report_date: row.report_date, status: "error", error: message.slice(0, 200) };
  };

  const todayLocal = localDay(tz, now);
  const daysAgo = Math.round((Date.parse(todayLocal) - Date.parse(row.report_date)) / DAY_MS);
  if (daysAgo < 1 || daysAgo > 20)
    return await markError(`report_date ${row.report_date} out of export range (days_ago=${daysAgo})`);

  // 1+2) resolve every export this report needs inside one bounded budget.
  // Stored ids are polled first; Dialpad expires ids after ~1 h, and the
  // shared client re-initiates an expired one on the spot (results are
  // cached by parameters, so that normally completes immediately).
  const needed = neededExports(daysAgo, callcenterId, tz);
  let stored: Record<string, string> = {};
  try {
    stored = row.export_ids ? JSON.parse(row.export_ids) : {};
  } catch {
    stored = {};
  }
  let fetched: Awaited<ReturnType<typeof fetchExports>>;
  try {
    fetched = await fetchExports(
      env.DIALPAD_API_KEY!, needed, stored, fetchImpl, testOpts.pollAttempts, testOpts.pollSpacingMs
    );
  } catch (err) {
    return await markError(`export: ${String((err as any)?.message ?? err)}`, { export_ids: stored });
  }
  // persist only the selectors this report still needs (stale keys from a
  // previous days_ago drop out) — the resume handle for the next tick
  const exportIds: Record<string, string> = {};
  for (const key of Object.keys(needed)) if (fetched.ids[key]) exportIds[key] = fetched.ids[key];
  if (JSON.stringify(exportIds) !== JSON.stringify(stored) || row.status !== "fetching") {
    await db
      .prepare("UPDATE qa_eod_reports SET status = 'fetching', export_ids = ?, updated_at = ? WHERE id = ?")
      .bind(JSON.stringify(exportIds), nowIso, row.id)
      .run();
  }
  const csvs = fetched.csvs;
  const missing = Object.keys(needed).filter((k) => !(k in csvs));
  if (missing.length)
    return {
      report_date: row.report_date,
      status: "fetching",
      note: `export not ready (${missing.join(",")}) — next tick resumes`,
      ...(fetched.reinitiated.length ? { reinitiated: fetched.reinitiated } : {}),
    };

  // 3) compute
  const pick = (prefix: string) =>
    Object.entries(csvs).filter(([k]) => k.startsWith(prefix)).flatMap(([, text]) => csvRecords(text));
  const cc = await fetchCallCenter(env.DIALPAD_API_KEY!, callcenterId, fetchImpl, {
    slSeconds: cfg.sl_seconds_fallback ?? 30,
    slTargetPct: cfg.sl_target_pct_fallback ?? 80,
  });
  const shifts = cfg.shifts?.length ? cfg.shifts : DEFAULT_SHIFTS;
  const generatedAt = `${localDay(tz, now)} ${new Intl.DateTimeFormat("en-GB", { timeZone: tz, hour: "2-digit", minute: "2-digit", hour12: false }).format(now)} ${tz}`;
  const report = buildReport(
    row.report_date,
    { records: pick("calls:"), duty: pick("onduty:"), daily: pick("daily:"), users: pick("users:") },
    { slSeconds: cc.slSeconds, slTargetPct: cc.slTargetPct, shortAbandonS: cfg.short_abandon_s ?? 6, shifts, generatedAt }
  );
  const audit = {
    report_date: row.report_date,
    summary: report.summary,
    service_level: cc,
    export_ids: exportIds,
    reinitiated: fetched.reinitiated,
    generated_at: generatedAt,
  };

  // 4) sink — a sheet that did not get written is the failure
  if (!env.GSHEETS_SA_JSON) return await markError("no sheets credentials (GSHEETS_SA_JSON)", audit);
  if (!cfg.spreadsheet_id) return await markError("eod_sheet.spreadsheet_id not configured", audit);
  let sheet: Record<string, number>;
  try {
    const client = await openSpreadsheet(env.GSHEETS_SA_JSON, cfg.spreadsheet_id, fetchImpl, now.getTime());
    const tabs = await listTabs(client);
    const dates = new Set([row.report_date]);
    const order = new Map<string, number>([["Full day", 0], ...shifts.map((s, i) => [s.label, i + 1] as [string, number])]);
    const key2 = (r: CellValue[]) => `${r[0]}|${r[1]}`;
    sheet = {
      summary_rows: await upsertTab(client, SUMMARY_TAB, SUMMARY_HEADER, report.summaryRows, dates,
        (r) => `${r[0]}|${String(order.get(String(r[1])) ?? 9)}`, tabs),
      calls_rows: await upsertTab(client, CALLS_TAB, CALLS_HEADER, report.callRows, dates, key2, tabs),
      agents_rows: await upsertTab(client, AGENTS_TAB, AGENTS_HEADER, report.agentRows, dates, key2, tabs),
    };
  } catch (err) {
    return await markError(`sheets: ${String((err as any)?.message ?? err)}`, audit);
  }

  const final = { ...audit, sheet, status: "completed" };
  await db
    .prepare("UPDATE qa_eod_reports SET status = 'completed', report = ?, updated_at = ? WHERE id = ?")
    .bind(JSON.stringify(final), nowIso, row.id)
    .run();
  return {
    report_date: row.report_date,
    status: "completed",
    full_day: report.summary.windows["Full day"],
    sheet,
  };
}

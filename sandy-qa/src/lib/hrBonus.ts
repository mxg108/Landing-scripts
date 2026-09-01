// HR bonus month payload — port of backend/services/hr_bonus_service.py
// (HRBonusSheet.md §4–§5). Builds the JSON the GAS renderer consumes 1:1;
// every number is computed here (aggregation, half-even rounding, display
// strings, ordering) so the GAS layer stays math-free. Population and
// semantics mirror the Railway read path: state='finalized' rows, call
// time = COALESCE(call_connected_at, created_at), month bucketing in the
// project bucket TZ (America/Los_Angeles).
//
// Transport note (Sandy era): Railway's GAS suite PULLED this payload;
// the SSO wall blocks that until Engineering provisions an inbound
// sast_ token, so the EOM cron PUSHES it to the GAS webapp instead
// (payload mode — the same direction flip every other GAS surface got).
// The GET route stays for humans and for the token era.

import { pyRound } from "./teamStats.js";
import type { TeamConfig } from "./teamConfig.js";

const BUCKET_TZ = "America/Los_Angeles";

// HR workbook display for binary section values (mockup parity — the HR
// sheet shows "N/A", not the Analyst_History projection's "Not Applicable").
const BINARY_DISPLAY: Record<string, string> = { Y: "Yes", N: "No", NA: "N/A" };
const BINARY_AS_FRACTION: Record<string, number> = { Y: 1, N: 0 };

const NUMERIC_TYPES = new Set(["numeric", "manual"]);

export interface HrExportConfig {
  sections: { id: string; hr_label: string }[];
  excluded_agents: string[];
}

export interface HrEvalRow {
  id: number;
  agent_name_raw: string;
  agent_email: string | null;
  evaluator_email: string | null;
  ts: string; // ISO — COALESCE(call_connected_at, created_at)
  overall_score: number | null;
  dialpad_link: string | null;
}

export type SectionsByEval = Record<
  number,
  Record<string, { numeric_score: number | null; binary_value: string | null }>
>;

// ── TZ helpers (per-instant offset — DST-correct for LA) ───────────────────

function tzOffsetMs(tz: string, atMs: number): number {
  const parts: Record<string, string> = {};
  new Intl.DateTimeFormat("en-US", {
    timeZone: tz,
    hour12: false,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  })
    .formatToParts(new Date(atMs))
    .forEach((p) => (parts[p.type] = p.value));
  const rendered = Date.UTC(
    Number(parts.year),
    Number(parts.month) - 1,
    Number(parts.day),
    Number(parts.hour === "24" ? "0" : parts.hour),
    Number(parts.minute),
    Number(parts.second)
  );
  return rendered - Math.trunc(atMs / 1000) * 1000;
}

// Local wall time in tz → UTC ms. One offset iteration is exact for
// month boundaries (DST flips at 2 AM, never midnight on the 1st).
function wallToUtcMs(tz: string, y: number, mon1: number): number {
  const guess = Date.UTC(y, mon1 - 1, 1);
  return guess - tzOffsetMs(tz, guess);
}

// [start, end) of a "YYYY-MM" month in the bucket TZ, as UTC ms — a call
// at 03:33 UTC on June 1 belongs to May (same seam as team_stats).
export function monthWindowUtc(month: string): [number, number] {
  const y = Number(month.slice(0, 4));
  const m = Number(month.slice(5, 7));
  const start = wallToUtcMs(BUCKET_TZ, y, m);
  const end = m === 12 ? wallToUtcMs(BUCKET_TZ, y + 1, 1) : wallToUtcMs(BUCKET_TZ, y, m + 1);
  return [start, end];
}

function laParts(ms: number): Record<string, string> {
  const parts: Record<string, string> = {};
  new Intl.DateTimeFormat("en-US", {
    timeZone: BUCKET_TZ,
    hour12: false,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  })
    .formatToParts(new Date(ms))
    .forEach((p) => (parts[p.type] = p.value));
  if (parts.hour === "24") parts.hour = "00";
  return parts;
}

const MONTH_NAMES = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];

// ── pure cells (hr_bonus_service parity) ───────────────────────────────────

function detailCell(
  row: { numeric_score: number | null; binary_value: string | null } | undefined
): string {
  if (!row) return "";
  if (row.numeric_score !== null && row.numeric_score !== undefined)
    return String(row.numeric_score);
  if (row.binary_value !== null && row.binary_value !== undefined)
    return BINARY_DISPLAY[row.binary_value] ?? "N/A";
  return "";
}

function summaryCell(
  scoreType: string,
  rows: ({ numeric_score: number | null; binary_value: string | null } | undefined)[]
): string {
  if (NUMERIC_TYPES.has(scoreType)) {
    const values = rows
      .filter((r) => r && r.numeric_score !== null && r.numeric_score !== undefined)
      .map((r) => Number(r!.numeric_score));
    // Python f"{mean:.2f}" formats the true binary value half-even —
    // pyRound is the proven port of that semantics.
    return values.length
      ? pyRound(values.reduce((a, b) => a + b, 0) / values.length, 2).toFixed(2)
      : "";
  }
  const values = rows
    .filter((r) => r && r.binary_value !== null && r.binary_value! in BINARY_AS_FRACTION)
    .map((r) => BINARY_AS_FRACTION[r!.binary_value!]);
  return values.length
    ? `${pyRound((values.reduce((a, b) => a + b, 0) / values.length) * 100, 0).toFixed(0)}%`
    : "";
}

// ── the builder (pure — rows in, payload out; golden-parity-tested vs
//    the Python original) ──────────────────────────────────────────────────

export function buildPayloadFromRows(
  config: TeamConfig,
  hr: HrExportConfig,
  month: string,
  evalRows: HrEvalRow[],
  sectionsByEval: SectionsByEval
): any {
  const [startMs, endMs] = monthWindowUtc(month);
  const excluded = new Set(hr.excluded_agents.map((n) => n.toLowerCase()));
  const idToType = new Map(config.sections_by_number.map((s: any) => [s.id, s.score_type]));
  const sectionDefs = hr.sections.map((e) => ({
    id: e.id,
    scoreType: String(idToType.get(e.id) ?? "numeric"),
  }));

  // The no-prior-month-leakage rule is load-bearing (owner-confirmed
  // 2026-07-07): the window re-check holds regardless of the SQL caller.
  const byAgent = new Map<string, (HrEvalRow & { tsMs: number })[]>();
  for (const ev of evalRows) {
    const tsMs = Date.parse(ev.ts);
    if (!Number.isFinite(tsMs) || tsMs < startMs || tsMs >= endMs) continue;
    const name = (ev.agent_name_raw ?? "").trim();
    if (!name || excluded.has(name.toLowerCase())) continue;
    const list = byAgent.get(name) ?? [];
    list.push({ ...ev, tsMs });
    byAgent.set(name, list);
  }

  const agents = [...byAgent.keys()]
    .sort((a, b) => (a.toLowerCase() < b.toLowerCase() ? -1 : a.toLowerCase() > b.toLowerCase() ? 1 : 0))
    .map((name) => {
      const evals = [...byAgent.get(name)!].sort((a, b) => b.tsMs - a.tsMs);
      const email = evals.find((e) => (e.agent_email ?? "").trim())?.agent_email?.trim() ?? "";
      const overall = evals
        .filter((e) => e.overall_score !== null && e.overall_score !== undefined)
        .map((e) => Number(e.overall_score));
      const rowsPerSection = new Map(
        sectionDefs.map((d) => [d.id, evals.map((e) => sectionsByEval[e.id]?.[d.id])])
      );
      return {
        name,
        email,
        monthly_avg: overall.length
          ? pyRound(overall.reduce((a, b) => a + b, 0) / overall.length, 1)
          : "",
        section_summaries: sectionDefs.map((d) => summaryCell(d.scoreType, rowsPerSection.get(d.id)!)),
        evaluations: evals.map((e) => {
          const p = laParts(e.tsMs);
          return {
            period: `${p.year}-${p.month}`,
            date: `${p.month}/${p.day}/${p.year} ${p.hour}:${p.minute}`,
            overall_score:
              e.overall_score !== null && e.overall_score !== undefined
                ? Number(e.overall_score)
                : "",
            sections: sectionDefs.map((d) => detailCell(sectionsByEval[e.id]?.[d.id])),
            evaluator: e.evaluator_email ?? "",
            dialpad_link: e.dialpad_link ?? "",
          };
        }),
      };
    });

  return {
    team_id: config.team_id,
    month,
    month_label: `${MONTH_NAMES[Number(month.slice(5, 7)) - 1]} ${month.slice(0, 4)}`,
    generated_at: new Date().toISOString(),
    section_labels: hr.sections.map((e) => e.hr_label),
    agents,
  };
}

// ── D1 fetch (the asyncpg wrapper's counterpart) ───────────────────────────

export function hrExportFor(hrExportJson: string | null): HrExportConfig | null {
  if (!hrExportJson) return null;
  try {
    const parsed = JSON.parse(hrExportJson);
    return Array.isArray(parsed?.sections) ? parsed : null;
  } catch {
    return null;
  }
}

export async function fetchMonthPayload(
  db: D1Database,
  config: TeamConfig,
  hr: HrExportConfig,
  month: string
): Promise<any> {
  const [startMs, endMs] = monthWindowUtc(month);
  // Coarse SQL prefilter ±2 days on the ISO text (synced rows may carry
  // +00:00 offsets that break lexicographic exactness) — the builder's
  // Date.parse window is the exact, load-bearing filter.
  const lo = new Date(startMs - 2 * 86_400_000).toISOString();
  const hi = new Date(endMs + 2 * 86_400_000).toISOString();
  const evalRows = (
    await db
      .prepare(
        `SELECT id, agent_name_raw, agent_email, evaluator_email,
                COALESCE(call_connected_at, created_at) AS ts,
                overall_score, dialpad_link
         FROM qa_evaluations
         WHERE team_id = ? AND state = 'finalized'
           AND COALESCE(call_connected_at, created_at) >= ?
           AND COALESCE(call_connected_at, created_at) < ?
         ORDER BY COALESCE(call_connected_at, created_at) DESC`
      )
      .bind(config.team_id, lo, hi)
      .all<any>()
  ).results as HrEvalRow[];

  const sectionsByEval: SectionsByEval = {};
  const sectionIds = hr.sections.map((e) => e.id);
  const secMarks = sectionIds.map(() => "?").join(",");
  for (let i = 0; i < evalRows.length; i += 40) {
    const chunk = evalRows.slice(i, i + 40);
    const evalMarks = chunk.map(() => "?").join(",");
    const rows = await db
      .prepare(
        `SELECT evaluation_id, section_id, numeric_score, binary_value
         FROM qa_evaluation_sections
         WHERE evaluation_id IN (${evalMarks}) AND section_id IN (${secMarks})`
      )
      .bind(...chunk.map((e) => e.id), ...sectionIds)
      .all<any>();
    for (const r of rows.results) {
      (sectionsByEval[r.evaluation_id] ??= {})[r.section_id] = {
        numeric_score: r.numeric_score ?? null,
        binary_value: r.binary_value ?? null,
      };
    }
  }
  return buildPayloadFromRows(config, hr, month, evalRows, sectionsByEval);
}

// ── GAS dispatch (payload mode — sofiaDigest posture) ──────────────────────

export async function dispatchHrBonus(payload: any, gasUrl?: string): Promise<any> {
  if (!gasUrl) return { status: "skipped", message: "GAS_WEBAPP_URL_HR not configured" };
  try {
    const res = await fetch(gasUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ hr_bonus: payload }),
      redirect: "follow",
      signal: AbortSignal.timeout(120_000),
    });
    const text = await res.text();
    try {
      return JSON.parse(text);
    } catch {
      return { status: "error", message: `non-JSON (HTTP ${res.status}): ${text.slice(0, 120)}` };
    }
  } catch (err) {
    return { status: "error", message: String((err as any)?.message ?? err).slice(0, 200) };
  }
}

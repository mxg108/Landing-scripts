// Port of backend/services/team_stats.py — pure computation, no I/O.
// Pandas/numpy semantics reproduced deliberately:
//   - round(x, d) is Python round-half-EVEN (banker's), not toFixed
//   - Series.std / np.std(ddof=1) — sample std
//   - np.median — average of the two middle values on even n
//   - Series.ewm(span).mean() — adjust=True weighting
//   - groupby iterates keys in SORTED order; sorts are stable (ES2019)
//   - month bucketing converts naive-UTC to America/Los_Angeles (BUCKET_TZ)
// `now` is injectable everywhere time enters — the golden-fixture parity
// harness pins it to the instant the Python fixture was captured.

import type { FrameRow } from "./historyFrame.js";
import type { StatsConfig, TeamConfig } from "./teamConfig.js";

export const BUCKET_TZ = "America/Los_Angeles";

const GAP_LOW_FRAC = 0.125;
const GAP_MED_FRAC = 0.25;
const GAP_HIGH_FRAC = 0.375;

// ── numeric helpers ─────────────────────────────────────────────────────────

export function pyRound(x: number, d = 0): number {
  if (!Number.isFinite(x)) return x;
  // Python's round() rounds the TRUE binary value, half-even only on exact
  // ties. An epsilon on x*10^d misclassifies near-halves (82.949999999999)
  // as ties — instead read the exact decimal expansion via toFixed and
  // decide from the digits beyond position d.
  const neg = x < 0;
  const abs = Math.abs(x);
  const s = abs.toFixed(Math.min(20, d + 18));
  const dot = s.indexOf(".");
  const intPart = s.slice(0, dot);
  const frac = s.slice(dot + 1);
  const kept = frac.slice(0, d);
  const rest = frac.slice(d);
  let base = BigInt(intPart + kept); // value scaled by 10^d, truncated
  const half = "5" + "0".repeat(rest.length - 1);
  if (rest > half) base += 1n;
  else if (rest === half) {
    if (base % 2n === 1n) base += 1n; // half-even
  }
  const out = Number(base) / 10 ** d;
  const signed = neg ? -out : out;
  return signed === 0 ? 0 : signed;
}

// numpy's pairwise summation (umath pairwise_sum, blocksize 128, 8-way
// unroll) — reproduced so float accumulation order matches the oracle
// bit-for-bit; sequential reduce differs by ulps and flips values sitting
// exactly on a rounding boundary (observed: one roster mean at 80.35).
function npSum(a: number[], lo = 0, n = a.length): number {
  if (n < 8) {
    let res = 0;
    for (let i = lo; i < lo + n; i++) res += a[i];
    return res;
  }
  if (n <= 128) {
    const r = [
      a[lo], a[lo + 1], a[lo + 2], a[lo + 3],
      a[lo + 4], a[lo + 5], a[lo + 6], a[lo + 7],
    ];
    let i = 8;
    for (; i + 8 <= n; i += 8)
      for (let j = 0; j < 8; j++) r[j] += a[lo + i + j];
    let res = ((r[0] + r[1]) + (r[2] + r[3])) + ((r[4] + r[5]) + (r[6] + r[7]));
    for (; i < n; i++) res += a[lo + i];
    return res;
  }
  let n2 = n >> 1;
  n2 -= n2 % 8;
  return npSum(a, lo, n2) + npSum(a, lo + n2, n - n2);
}

function mean(xs: number[]): number {
  return npSum(xs) / xs.length;
}

function stdSample(xs: number[]): number {
  const m = mean(xs);
  return Math.sqrt(npSum(xs.map((x) => (x - m) ** 2)) / (xs.length - 1));
}

function median(xs: number[]): number {
  const s = [...xs].sort((a, b) => a - b);
  const mid = s.length >> 1;
  return s.length % 2 ? s[mid] : (s[mid - 1] + s[mid]) / 2;
}

// pandas ewm(span).mean() with adjust=True:
// y_t = Σ_{i=0..t} (1-α)^i · x_{t-i} / Σ_{i=0..t} (1-α)^i, α = 2/(span+1)
function ewmaSeries(xs: number[], span: number): number[] {
  const alpha = 2 / (span + 1);
  const out: number[] = [];
  let num = 0;
  let den = 0;
  const decay = 1 - alpha;
  for (const x of xs) {
    num = num * decay + x;
    den = den * decay + 1;
    out.push(num / den);
  }
  return out;
}

function groupByAgent(rows: FrameRow[]): Map<string, FrameRow[]> {
  const groups = new Map<string, FrameRow[]>();
  for (const r of rows) {
    let g = groups.get(r.agent);
    if (!g) groups.set(r.agent, (g = []));
    g.push(r);
  }
  return new Map([...groups.entries()].sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0)));
}

// ── month bucketing (BUCKET_TZ seam) ────────────────────────────────────────

const _ymFmt = new Intl.DateTimeFormat("en-CA", {
  timeZone: BUCKET_TZ,
  year: "numeric",
  month: "2-digit",
});

export function monthInBucketTz(tsMs: number): string {
  return _ymFmt.format(new Date(tsMs)).slice(0, 7); // "YYYY-MM"
}

function currentAndLastYm(now: Date): { current: string; last: string } {
  const current = monthInBucketTz(now.getTime());
  const [y, m] = current.split("-").map(Number);
  const lastY = m === 1 ? y - 1 : y;
  const lastM = m === 1 ? 12 : m - 1;
  return { current, last: `${lastY}-${String(lastM).padStart(2, "0")}` };
}

// ── the compute_* ports ─────────────────────────────────────────────────────

export function computeOutliers(rows: FrameRow[], stats: StatsConfig): any[] {
  if (!rows.length) return [];
  const results: any[] = [];
  for (const [agent, group] of groupByAgent(rows)) {
    if (group.length < stats.min_evals_for_outlier) continue;
    const scores = group.map((r) => r.overall_score);
    const med = median(scores);
    const mad = median(scores.map((s) => Math.abs(s - med)));
    if (mad === 0) continue;
    for (const row of group) {
      const z = (0.6745 * (row.overall_score - med)) / mad;
      if (Math.abs(z) > stats.outlier_z_threshold) {
        results.push({
          agent,
          date: new Date(row.ts).toISOString().slice(0, 10),
          score: pyRound(row.overall_score, 1),
          agent_median: pyRound(med, 1),
          modified_z: pyRound(z, 2),
          classification: z > 0 ? "exceptional" : "concerning",
          eval_id: row.eval_id,
        });
      }
    }
  }
  results.sort((a, b) => Math.abs(b.modified_z) - Math.abs(a.modified_z));
  return results;
}

export function computeEwma(rows: FrameRow[], stats: StatsConfig): any[] {
  if (!rows.length) return [];
  const { ewma_span: span, ewma_min_evals: minEvals, ewma_trend_delta: delta } = stats;
  const results: any[] = [];
  for (const [agent, groupUnsorted] of groupByAgent(rows)) {
    const group = [...groupUnsorted].sort((a, b) => a.ts - b.ts);
    if (group.length < minEvals) continue;
    const series = ewmaSeries(group.map((r) => r.overall_score), span);
    const current = series[series.length - 1];
    const past = series[Math.max(0, series.length - span - 1)];
    const diff = current - past;
    results.push({
      agent,
      current_ewma: pyRound(current, 1),
      trend: diff > delta ? "improving" : diff < -delta ? "declining" : "flat",
      eval_count: group.length,
    });
  }
  results.sort((a, b) => a.current_ewma - b.current_ewma);
  return results;
}

export function computeMonthlySummary(rows: FrameRow[], now: Date): any {
  const { current: currentYm, last: lastYm } = currentAndLastYm(now);
  const empty = (ym: string) => ({ year_month: ym, count: 0, mean: null, std: null });
  if (!rows.length) return { last: empty(lastYm), current: empty(currentYm) };
  const months = rows.map((r) => monthInBucketTz(r.ts));
  const bucket = (ym: string) => {
    const scores = rows.filter((_, i) => months[i] === ym).map((r) => r.overall_score);
    if (!scores.length) return empty(ym);
    return {
      year_month: ym,
      count: scores.length,
      mean: pyRound(mean(scores), 1),
      std: scores.length > 1 ? pyRound(stdSample(scores), 1) : null,
    };
  };
  return { last: bucket(lastYm), current: bucket(currentYm) };
}

export function computeMonthlySpc(rows: FrameRow[], stats: StatsConfig): any {
  if (!rows.length) return { months: [], ucl: 0, lcl: 0, center: 0 };
  const byMonth = new Map<string, number[]>();
  for (const r of rows) {
    const ym = monthInBucketTz(r.ts);
    let g = byMonth.get(ym);
    if (!g) byMonth.set(ym, (g = []));
    g.push(r.overall_score);
  }
  const sorted = [...byMonth.entries()].sort(([a], [b]) => (a < b ? -1 : 1));
  const means = sorted.map(([, scores]) => mean(scores));
  const center = mean(means);
  const sigma = means.length > 1 ? stdSample(means) : 0;
  const k = stats.spc_sigma_multiplier;
  return {
    months: sorted.map(([ym, scores]) => ({
      month: ym,
      mean: pyRound(mean(scores), 1),
      count: scores.length,
    })),
    ucl: pyRound(center + k * sigma, 1),
    lcl: pyRound(center - k * sigma, 1),
    center: pyRound(center, 1),
  };
}

function sectionRangeSize(config: TeamConfig, hid: string, dflt = 4): number {
  const sec = config.history_id_to_section[hid];
  if (sec?.score_range) return sec.score_range[1] - sec.score_range[0];
  return dflt;
}

function numVals(group: FrameRow[], hid: string): number[] {
  return group.map((r) => r.num[hid]).filter((v): v is number => v !== null && v !== undefined && !Number.isNaN(v));
}

export function computeSectionAnalysis(rows: FrameRow[], config: TeamConfig): any {
  if (!rows.length)
    return { team_means: {}, team_stds: {}, training_opportunities: [] };
  const teamMeansById: Record<string, number> = {};
  const teamMeansRounded: Record<string, number> = {};
  const teamStds: Record<string, number> = {};
  for (const hid of config.numeric_history_ids) {
    const vals = numVals(rows, hid);
    teamMeansById[hid] = vals.length ? mean(vals) : 0;
    teamMeansRounded[hid] = vals.length ? pyRound(mean(vals), 2) : 0;
    teamStds[hid] = vals.length > 1 ? pyRound(stdSample(vals), 2) : 0;
  }
  const opportunities: any[] = [];
  for (const [agent, group] of groupByAgent(rows)) {
    for (const hid of config.numeric_history_ids) {
      const agentVals = numVals(group, hid);
      if (agentVals.length < 2) continue;
      const agentAvg = mean(agentVals);
      // Python compares against the ROUNDED team mean (team_means dict holds
      // round(...,2) values) — keep that quirk for parity.
      const tAvg = teamMeansRounded[hid] ?? 0;
      const gap = tAvg - agentAvg;
      const rangeSize = sectionRangeSize(config, hid);
      if (gap > GAP_LOW_FRAC * rangeSize) {
        const priority =
          gap >= GAP_HIGH_FRAC * rangeSize
            ? "high"
            : gap >= GAP_MED_FRAC * rangeSize
              ? "medium"
              : "low";
        opportunities.push({
          agent,
          section: config.section_labels[hid] ?? hid,
          agent_avg: pyRound(agentAvg, 2),
          team_avg: pyRound(tAvg, 2),
          gap: pyRound(gap, 2),
          n: agentVals.length,
          priority,
        });
      }
    }
  }
  opportunities.sort((a, b) => b.gap - a.gap);
  return {
    team_means: Object.fromEntries(
      config.numeric_history_ids.map((hid) => [
        config.section_labels[hid] ?? hid,
        teamMeansRounded[hid],
      ])
    ),
    team_stds: Object.fromEntries(
      config.numeric_history_ids.map((hid) => [
        config.section_labels[hid] ?? hid,
        teamStds[hid],
      ])
    ),
    training_opportunities: opportunities,
  };
}

function binaryPct(vals: string[]): number {
  const yes = vals.filter((v) => v === "Y").length;
  const total = vals.filter((v) => v === "Y" || v === "N").length;
  return total > 0 ? pyRound((100.0 * yes) / total, 1) : 0.0;
}

export function computeBinaryStats(
  rows: FrameRow[],
  ynSectionLabels: Record<string, string>
): any[] {
  const result: any[] = [];
  for (const [sectionId, label] of Object.entries(ynSectionLabels)) {
    if (!rows.length) {
      result.push({ section_id: sectionId, label, team_pct: 0.0, agents: [] });
      continue;
    }
    const teamPct = binaryPct(rows.map((r) => r.yn[sectionId] ?? ""));
    const agents: any[] = [];
    for (const [agent, group] of groupByAgent(rows)) {
      const vals = group.map((r) => r.yn[sectionId] ?? "");
      const total = vals.filter((v) => v === "Y" || v === "N").length;
      if (total > 0) {
        agents.push({
          agent,
          pct: binaryPct(vals),
          yes: vals.filter((v) => v === "Y").length,
          total,
        });
      }
    }
    agents.sort((a, b) => a.pct - b.pct);
    result.push({ section_id: sectionId, label, team_pct: teamPct, agents });
  }
  return result;
}

export function computeSupervisorStats(rows: FrameRow[]): any[] {
  const filtered = rows.filter((r) => r.supervisor.trim() !== "");
  if (!filtered.length) return [];
  const groups = new Map<string, FrameRow[]>();
  for (const r of filtered) {
    let g = groups.get(r.supervisor);
    if (!g) groups.set(r.supervisor, (g = []));
    g.push(r);
  }
  const results: any[] = [];
  for (const [sup, group] of [...groups.entries()].sort(([a], [b]) => (a < b ? -1 : 1))) {
    const scores = group.map((r) => r.overall_score);
    results.push({
      supervisor: sup,
      avg_score: pyRound(mean(scores), 1),
      std_score: scores.length > 1 ? pyRound(stdSample(scores), 1) : 0,
      eval_count: group.length,
      agent_count: new Set(group.map((r) => r.agent)).size,
    });
  }
  results.sort((a, b) => b.avg_score - a.avg_score);
  return results;
}

export function computeAgentRoster(rows: FrameRow[], config: TeamConfig): any[] {
  if (!rows.length) return [];
  const teamMeans: Record<string, number> = {};
  for (const hid of config.numeric_history_ids) {
    const vals = numVals(rows, hid);
    teamMeans[hid] = vals.length ? mean(vals) : 0;
  }
  const ewmaLookup = new Map(
    computeEwma(rows, config.stats).map((e) => [e.agent, e])
  );
  const results: any[] = [];
  for (const [agent, group] of groupByAgent(rows)) {
    const scores = group.map((r) => r.overall_score);
    const meanScore = mean(scores);
    const stdScore = scores.length > 1 ? stdSample(scores) : 0;
    const ewmaData = ewmaLookup.get(agent);
    const ewmaVal = ewmaData ? ewmaData.current_ewma : null;
    const trend = ewmaData ? ewmaData.trend : "flat";
    const ref = ewmaVal !== null ? ewmaVal : meanScore;
    const status =
      ref >= 90 ? "excellent" : ref >= 80 ? "good" : ref >= 70 ? "watch" : "at_risk";
    const binaryPcts: Record<string, number> = {};
    for (const ynId of config.yn_history_ids)
      binaryPcts[ynId] = binaryPct(group.map((r) => r.yn[ynId] ?? ""));
    const weak: string[] = [];
    for (const hid of config.numeric_history_ids) {
      const agentVals = numVals(group, hid);
      if (agentVals.length < 2) continue;
      if (
        (teamMeans[hid] ?? 0) - mean(agentVals) >
        GAP_LOW_FRAC * sectionRangeSize(config, hid)
      )
        weak.push(config.section_labels[hid] ?? hid);
    }
    results.push({
      agent,
      n: group.length,
      mean: pyRound(meanScore, 1),
      std: pyRound(stdScore, 1),
      ewma: ewmaVal,
      trend,
      binary_pcts: binaryPcts,
      status,
      weak_sections: weak,
      is_active: group[0].is_active,
      supervisor: group[0].supervisor,
    });
  }
  results.sort((a, b) => b.mean - a.mean);
  return results;
}

export function computeDistribution(rows: FrameRow[]): any[] {
  if (!rows.length) return [];
  const bins: [number, number, string][] = [
    [0, 20, "0-20"],
    [21, 40, "21-40"],
    [41, 60, "41-60"],
    [61, 70, "61-70"],
    [71, 80, "71-80"],
    [81, 90, "81-90"],
    [91, 100, "91-100"],
  ];
  return bins.map(([lo, hi, label]) => ({
    bin: label,
    count: rows.filter((r) => r.overall_score >= lo && r.overall_score <= hi).length,
  }));
}

// ── response assembly (route logic from backend/routes/team.py) ─────────────

export interface StatsFilters {
  days: number;
  active_only: boolean;
  supervisor: string;
  date_from: string | null;
  date_to: string | null;
}

export function assembleTeamStats(
  frame: FrameRow[],
  config: TeamConfig,
  filters: StatsFilters,
  now: Date
): any {
  const base = {
    team_id: config.team_id,
    rubric_version: config.rubric_version,
    generated_at: now.toISOString(),
    coverage_regime: "manager_sample",
    filters_applied: filters,
  };
  if (!frame.length) {
    return {
      ...base,
      kpis: { total_evals: 0, avg_score: 0, std_score: 0, analyst_count: 0 },
      monthly: computeMonthlySummary([], now),
      roster: [],
      outliers: [],
      spc: { months: [], ucl: 0, lcl: 0, center: 0 },
      section_analysis: { team_means: {}, team_stds: {}, training_opportunities: [] },
      binary_stats: Object.entries(config.yn_section_labels).map(([sid, label]) => ({
        section_id: sid,
        label,
        team_pct: 0.0,
        agents: [],
      })),
      supervisor_stats: [],
      ewma: [],
      distribution: [],
    };
  }

  let df = frame;
  if (filters.active_only) df = df.filter((r) => r.is_active);
  if (filters.supervisor) {
    const sup = filters.supervisor.trim().toLowerCase();
    df = df.filter((r) => r.supervisor.toLowerCase() === sup);
  }

  // monthly chiclets honor active/supervisor but NOT the days filter
  const monthly = computeMonthlySummary(df, now);

  if (filters.date_from && filters.date_to) {
    const from = Date.parse(`${filters.date_from}T00:00:00Z`);
    const to = Date.parse(`${filters.date_to}T23:59:59.999Z`);
    df = df.filter((r) => r.ts >= from && r.ts <= to);
  } else if (filters.days > 0) {
    const cutoff = now.getTime() - filters.days * 86_400_000;
    df = df.filter((r) => r.ts >= cutoff);
  }

  const scores = df.map((r) => r.overall_score);
  const kpis = {
    total_evals: df.length,
    avg_score: df.length > 0 ? pyRound(mean(scores), 1) : 0,
    std_score: df.length > 1 ? pyRound(stdSample(scores), 1) : 0,
    analyst_count: new Set(df.map((r) => r.agent)).size,
  };

  return {
    ...base,
    kpis,
    monthly,
    roster: computeAgentRoster(df, config),
    outliers: computeOutliers(df, config.stats),
    spc: computeMonthlySpc(df, config.stats),
    section_analysis: computeSectionAnalysis(df, config),
    binary_stats: computeBinaryStats(df, config.yn_section_labels),
    supervisor_stats: computeSupervisorStats(df),
    ewma: computeEwma(df, config.stats),
    distribution: computeDistribution(df),
  };
}

export function assembleTeamEvals(
  frame: FrameRow[],
  config: TeamConfig,
  yearMonth: string,
  activeOnly: boolean,
  supervisor: string
): any {
  const filters = { active_only: activeOnly, supervisor };
  let df = frame;
  if (activeOnly) df = df.filter((r) => r.is_active);
  if (supervisor) {
    const sup = supervisor.trim().toLowerCase();
    df = df.filter((r) => r.supervisor.toLowerCase() === sup);
  }
  df = df.filter((r) => monthInBucketTz(r.ts) === yearMonth);
  df = [...df].sort((a, b) => b.ts - a.ts); // stable, newest first
  const iso = (ms: number | null) =>
    ms === null ? null : new Date(ms).toISOString();
  return {
    team_id: config.team_id,
    year_month: yearMonth,
    rows: df.map((r) => ({
      agent: r.agent,
      timestamp: iso(r.ts),
      eval_approved_at: iso(r.eval_approved_at),
      overall_score: r.overall_score,
      dialpad_link: r.eval_id
        ? `https://dialpad.com/callhistory/callreview/${r.eval_id}`
        : "",
      eval_id: r.eval_id,
      supervisor: r.supervisor || null,
      evaluator_email: r.manager_email || null,
    })),
    filters_applied: filters,
  };
}

// Daily Agent QA Digest — references/DailyDigest.md.
//
// One email per active agent per sweep night: per-section averages for the
// night vs the month, a Sonnet-written trend summary (qa-insights, judge
// parity), the ProgressionCard strip (last 5 + tonight) and the plain
// month-to-date average (HR-bonus window). Replaces the per-call scorecard
// emails the nightly sweep suppresses (NightlyScoring §0.2).
//
// Layout: facts (deterministic, persisted) → prompt → summary persist
// (callback) → HTML rendered HERE → GAS {html_email} as a pure sender.
// The cron-job handler at the bottom walks the phases one bounded step at
// a time (CronContinuation §2.6), sleeping via `wait_s` while it waits.

import type { TeamConfig, SectionDef } from "./teamConfig.js";
import type { JobHandler } from "./cronJobs.js";

export interface DigestConfig {
  enabled?: boolean;
  send_deadline_utc?: number; // decimal UTC hour, e.g. 13.5 = 13:30
  model?: string;
  max_tokens?: number;
  cc_supervisor?: boolean;
  summary_timeout_min?: number;
}

const DEFAULT_MODEL = "claude-sonnet-5";
const DEFAULT_MAX_TOKENS = 6000;
const DEFAULT_DEADLINE_UTC = 13.5;
const DEFAULT_SUMMARY_TIMEOUT_MIN = 20;
const AWAIT_SCORES_WAIT_S = 300;
const AWAIT_SUMMARY_WAIT_S = 60;
const INSIGHTS_MAX_ITEMS = 40; // qa-insights MAX_ITEMS
const RECENT_N = 5; // ProgressionCard MAX_HISTORY
const ID_CHUNK = 50; // D1 bound-parameter cap ~100
const BASE_URL = "https://qa-scoring.sandy.hellolanding.tech";

// GAS Branding.js palette + thresholds (Member Support); kept here so the
// digest matches the per-call scorecard byte-for-byte in look.
const C = {
  DARK_NAVY: "#15192D",
  ACCENT_BLUE: "#1A61D9",
  LIGHT_BLUE: "#E7EFFB",
  WHITE: "#FFFFFF",
  AMBER: "#E8A317",
  RED: "#D9534F",
  GREEN: "#28A745",
  TEXT_GRAY: "#4A4A4A",
  GOLD: "#FFD700",
  BORDER: "#E0E0E0",
  BG: "#F5F5F5",
};
const OVERALL_HIGH = 85;
const OVERALL_MID = 70;
const CATEGORY_HIGH = 4.25;
const CATEGORY_MID = 3.5;

export function digestConfigFor(providerConfig: string | null | undefined): DigestConfig | null {
  if (!providerConfig) return null;
  try {
    const cfg = JSON.parse(providerConfig);
    const d = cfg?.nightly_sweep?.digest;
    return d && d.enabled ? (d as DigestConfig) : null;
  } catch {
    return null;
  }
}

const r1 = (x: number) => Math.round(x * 10) / 10;
const mean = (xs: number[]) => (xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null);
const parseJson = (text: string | null | undefined, fallback: any) => {
  if (!text) return fallback;
  try {
    return JSON.parse(text);
  } catch {
    return fallback;
  }
};
const chunk = <T>(xs: T[], n: number): T[][] => {
  const out: T[][] = [];
  for (let i = 0; i < xs.length; i += n) out.push(xs.slice(i, i + n));
  return out;
};

export function assessedSections(config: TeamConfig): SectionDef[] {
  return config.sections_by_number.filter(
    (s) => !s.auto_value && !["manual", "manual_yn"].includes(s.score_type)
  );
}
const isYn = (s: SectionDef) => s.score_type.endsWith("yn");

// ── facts ──────────────────────────────────────────────────────────────────

export interface SectionStat {
  avg: number | null; // numeric sections: mean of scored (non-NA) values
  yes: number | null; // yn sections: % Y over scored (non-NA) values
  n: number;
}

export interface DigestFacts {
  team_id: string;
  pull_date: string;
  agent: { id: number; name: string; email: string };
  rubric_version: string;
  sections: { id: string; name: string; type: "numeric" | "yn"; range: [number, number] | null }[];
  tonight: {
    n: number;
    avg: number | null;
    evals: any[];
    section_stats: Record<string, SectionStat>;
  };
  month: {
    label: string; // YYYY-MM (America/Los_Angeles)
    n: number;
    avg: number | null;
    section_stats: Record<string, SectionStat>;
  };
  recent: { eval_id: number; ts: string; overall: number; tonight: boolean }[]; // oldest → newest
  pending_review: number;
}

function sectionStatsFrom(
  sections: SectionDef[],
  rows: { section_id: string; numeric_score: number | null; binary_value: string | null }[]
): Record<string, SectionStat> {
  const out: Record<string, SectionStat> = {};
  for (const s of sections) {
    const mine = rows.filter((r) => r.section_id === s.id && r.binary_value !== "NA");
    if (isYn(s)) {
      const scored = mine.filter((r) => r.binary_value === "Y" || r.binary_value === "N");
      const yes = scored.filter((r) => r.binary_value === "Y").length;
      out[s.id] = { avg: null, yes: scored.length ? r1((100 * yes) / scored.length) : null, n: scored.length };
    } else {
      const vals = mine.map((r) => r.numeric_score).filter((v): v is number => v != null);
      const m = mean(vals);
      out[s.id] = { avg: m == null ? null : Math.round(m * 100) / 100, yes: null, n: vals.length };
    }
  }
  return out;
}

async function sectionsForEvals(db: D1Database, evalIds: number[]): Promise<any[]> {
  const rows: any[] = [];
  for (const ids of chunk(evalIds, ID_CHUNK)) {
    const res = await db
      .prepare(
        `SELECT evaluation_id, section_id, numeric_score, binary_value, reasoning
         FROM qa_evaluation_sections WHERE evaluation_id IN (${ids.map(() => "?").join(",")})
         ORDER BY evaluation_id, section_number`
      )
      .bind(...ids)
      .all<any>();
    rows.push(...res.results);
  }
  return rows;
}

// Finalized evals among the night's picks, grouped by roster agent.
export async function nightCohort(
  db: D1Database,
  teamId: string,
  pickedCallIds: string[]
): Promise<{ finalized: any[]; pendingReviewByAgent: Record<number, number> }> {
  const finalized: any[] = [];
  const pendingReviewByAgent: Record<number, number> = {};
  for (const ids of chunk(pickedCallIds, ID_CHUNK)) {
    const res = await db
      .prepare(
        `SELECT id, agent_id, agent_name_raw, agent_email, state, scoring_status,
                overall_score, call_connected_at, created_at, dialpad_call_id,
                dialpad_disposition_category, dialpad_disposition, call_summary,
                key_strengths, opportunities, dialpad_call_metadata
         FROM qa_evaluations
         WHERE team_id = ? AND dialpad_call_id IN (${ids.map(() => "?").join(",")})`
      )
      .bind(teamId, ...ids)
      .all<any>();
    for (const ev of res.results) {
      if (ev.state === "finalized" && ev.overall_score != null && ev.agent_id != null) finalized.push(ev);
      else if (ev.scoring_status === "flagged_human_review" && ev.agent_id != null)
        pendingReviewByAgent[ev.agent_id] = (pendingReviewByAgent[ev.agent_id] ?? 0) + 1;
    }
  }
  return { finalized, pendingReviewByAgent };
}

function sopRefs(metadata: string | null): string[] {
  const m = parseJson(metadata, {});
  const docs = m?.pulpo_docs;
  if (Array.isArray(docs) && docs.length)
    return docs.map((d: any, i: number) => (d?.title ? `SOP ${i + 1}: ${d.title}` : "")).filter(Boolean);
  return m?.sop_used ? [String(m.sop_used)] : [];
}

export async function buildAgentFacts(
  db: D1Database,
  config: TeamConfig,
  teamId: string,
  pullDate: string,
  agent: { id: number; name: string; email: string },
  nightEvals: any[],
  pendingReview: number,
  nowMs: number
): Promise<DigestFacts> {
  const { monthWindowUtc } = await import("./hrBonus.js");
  const { monthInBucketTz } = await import("./teamStats.js");
  const sections = assessedSections(config);
  const secDefs = sections.map((s) => ({
    id: s.id,
    name: s.name,
    type: (isYn(s) ? "yn" : "numeric") as "yn" | "numeric",
    range: s.score_range,
  }));

  const tonight = [...nightEvals].sort(
    (a, b) => Date.parse(a.call_connected_at ?? a.created_at) - Date.parse(b.call_connected_at ?? b.created_at)
  );
  const tonightIds = tonight.map((e) => Number(e.id));
  const tonightSections = await sectionsForEvals(db, tonightIds);
  const bySecEval = new Map<number, any[]>();
  for (const r of tonightSections) {
    const list = bySecEval.get(r.evaluation_id) ?? [];
    list.push(r);
    bySecEval.set(r.evaluation_id, list);
  }
  const evals = tonight.map((e) => {
    const secs = bySecEval.get(Number(e.id)) ?? [];
    const scores: Record<string, number | string | null> = {};
    const reasoning: Record<string, string> = {};
    for (const s of sections) {
      const row = secs.find((r) => r.section_id === s.id);
      if (!row) continue;
      scores[s.id] = row.binary_value === "NA" ? "NA" : isYn(s) ? row.binary_value : row.numeric_score;
      if (row.reasoning) reasoning[s.id] = row.reasoning;
    }
    return {
      eval_id: Number(e.id),
      call_id: e.dialpad_call_id,
      ts: e.call_connected_at ?? e.created_at,
      overall: Number(e.overall_score),
      disposition: [e.dialpad_disposition_category, e.dialpad_disposition].filter(Boolean).join(" — "),
      call_summary: e.call_summary ?? "",
      strengths: e.key_strengths ?? "",
      opportunities: e.opportunities ?? "",
      sop_references: sopRefs(e.dialpad_call_metadata),
      scores,
      reasoning,
    };
  });
  const tonightAvg = mean(evals.map((e) => e.overall));

  // Month to date — the HR-bonus window (America/Los_Angeles calendar month).
  const label = monthInBucketTz(nowMs);
  const [startMs, endMs] = monthWindowUtc(label);
  const monthRows = (
    await db
      .prepare(
        `SELECT id, overall_score, COALESCE(call_connected_at, created_at) AS ts
         FROM qa_evaluations
         WHERE team_id = ? AND agent_id = ? AND state = 'finalized' AND overall_score IS NOT NULL
           AND COALESCE(call_connected_at, created_at) >= ? AND COALESCE(call_connected_at, created_at) < ?`
      )
      .bind(teamId, agent.id, new Date(startMs).toISOString(), new Date(endMs).toISOString())
      .all<any>()
  ).results;
  const monthAvg = mean(monthRows.map((r) => Number(r.overall_score)));
  const monthSections = await sectionsForEvals(
    db,
    monthRows.map((r) => Number(r.id))
  );

  // Progression strip: the last RECENT_N finalized evals BEFORE tonight's
  // earliest, then tonight's — oldest → newest (ProgressionCard reads it
  // chronologically and highlights the newest).
  const tonightSet = new Set(tonightIds);
  const earliestTonight = tonight.length ? (tonight[0].call_connected_at ?? tonight[0].created_at) : new Date(nowMs).toISOString();
  const prior = (
    await db
      .prepare(
        `SELECT id, overall_score, COALESCE(call_connected_at, created_at) AS ts
         FROM qa_evaluations
         WHERE team_id = ? AND agent_id = ? AND state = 'finalized' AND overall_score IS NOT NULL
           AND COALESCE(call_connected_at, created_at) < ?
         ORDER BY COALESCE(call_connected_at, created_at) DESC LIMIT ?`
      )
      .bind(teamId, agent.id, earliestTonight, RECENT_N + tonightIds.length)
      .all<any>()
  ).results
    .filter((r) => !tonightSet.has(Number(r.id)))
    .slice(0, RECENT_N)
    .reverse()
    .map((r) => ({ eval_id: Number(r.id), ts: r.ts, overall: Number(r.overall_score), tonight: false }));
  const recent = [
    ...prior,
    ...evals.map((e) => ({ eval_id: e.eval_id, ts: e.ts, overall: e.overall, tonight: true })),
  ];

  return {
    team_id: teamId,
    pull_date: pullDate,
    agent,
    rubric_version: config.rubric_version,
    sections: secDefs,
    tonight: {
      n: evals.length,
      avg: tonightAvg == null ? null : r1(tonightAvg),
      evals,
      section_stats: sectionStatsFrom(sections, tonightSections),
    },
    month: {
      label,
      n: monthRows.length,
      avg: monthAvg == null ? null : r1(monthAvg),
      section_stats: sectionStatsFrom(sections, monthSections),
    },
    recent,
    pending_review: pendingReview,
  };
}

// ── prompt ─────────────────────────────────────────────────────────────────

export interface DigestSummary {
  headline: string;
  sections: { section_id: string; trend: "up" | "down" | "flat" | "new"; note: string }[];
  focus: string;
}

export function buildDigestPrompts(facts: DigestFacts): { system: string; prompt: string } {
  const system =
    "You are the QA evaluator for Landing's Member Support call center, " +
    "writing the agent's nightly QA digest. You address the agent directly " +
    "in the second person (\"you\"), never the third person. Every claim " +
    "must trace to the fact sheet: cite the section scores and the " +
    "evaluator reasoning you rely on, and reference SOP steps by name when " +
    "the reasoning does. Do not transcribe the reasoning back — synthesize " +
    "the pattern across tonight's calls and against the month. Be " +
    "specific, warm and direct; no filler, no generic advice.";
  const secList = facts.sections.map((s) => `- ${s.id}: ${s.name} (${s.type})`).join("\n");
  const prompt =
    `FACT SHEET (deterministic, computed from the QA database):\n` +
    `${JSON.stringify(facts, null, 2)}\n\n` +
    `HOW TO READ IT: tonight.evals are the calls scored tonight (each with ` +
    `per-section scores and the evaluator's reasoning). tonight.section_stats ` +
    `and month.section_stats are the per-section averages for tonight and for ` +
    `the month to date. recent is the overall-score progression, oldest first; ` +
    `entries with tonight=true are tonight's.\n\n` +
    `SECTIONS (use these exact ids, every one exactly once):\n${secList}\n\n` +
    `Respond with ONLY a JSON object (no markdown fences, no prose outside it) ` +
    `in exactly this shape:\n` +
    `{"headline": "<1-2 sentences: how tonight went overall, with the overall ` +
    `score(s) and the month-to-date average>",\n` +
    ` "sections": [{"section_id": "<id>", "trend": "up"|"down"|"flat"|"new", ` +
    `"note": "<1-2 sentences: tonight vs month for this section, with figures, ` +
    `and the concrete behavior behind the number>"}],\n` +
    ` "focus": "<3-4 lines: the single most valuable thing to work on next ` +
    `shift, grounded in tonight's reasoning and the SOP steps it cites>"}\n` +
    `trend rules: "new" when the month has no prior evals for the section; ` +
    `"up"/"down" only when tonight differs from the month average by at least ` +
    `0.25 (numeric) or 15 points (yn); otherwise "flat".`;
  return { system, prompt };
}

const TRENDS = new Set(["up", "down", "flat", "new"]);

export function parseDigestSummary(facts: DigestFacts, text: string): DigestSummary {
  const t = (text ?? "").trim().replace(/^```(?:json)?\s*/i, "").replace(/\s*```$/, "");
  let parsed: any;
  try {
    parsed = JSON.parse(t);
  } catch {
    throw new Error("response is not JSON");
  }
  if (!parsed?.headline || !parsed?.focus || !Array.isArray(parsed.sections))
    throw new Error("response is not the expected JSON shape");
  const byId = new Map<string, any>(parsed.sections.map((s: any) => [s.section_id, s]));
  const sections = facts.sections.map((s) => {
    const out = byId.get(s.id);
    if (!out) throw new Error(`missing section '${s.id}'`);
    if (!TRENDS.has(out.trend)) throw new Error(`section '${s.id}': bad trend '${out.trend}'`);
    if (!out.note) throw new Error(`section '${s.id}': note required`);
    return { section_id: s.id, trend: out.trend, note: String(out.note) };
  });
  return { headline: String(parsed.headline), sections, focus: String(parsed.focus) };
}

// ── render (inline CSS — Gmail strips <style>) ─────────────────────────────

const esc = (s: unknown) =>
  String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");

const overallColor = (score: number) =>
  score >= 100 ? C.GOLD : score >= OVERALL_HIGH ? C.GREEN : score >= OVERALL_MID ? C.AMBER : C.RED;
const categoryColor = (v: number | null) =>
  v == null ? C.TEXT_GRAY : v >= CATEGORY_HIGH ? C.GREEN : v >= CATEGORY_MID ? C.AMBER : C.RED;
const ynColor = (pct: number | null) =>
  pct == null ? C.TEXT_GRAY : pct >= 85 ? C.GREEN : pct >= 70 ? C.AMBER : C.RED;

function fmtStat(type: "numeric" | "yn", st: SectionStat | undefined): { text: string; color: string } {
  if (!st || !st.n) return { text: "—", color: C.TEXT_GRAY };
  if (type === "yn") return { text: `${Math.round(st.yes ?? 0)}% Y`, color: ynColor(st.yes) };
  return { text: (st.avg ?? 0).toFixed(2), color: categoryColor(st.avg) };
}

const trendGlyph = (t: string) =>
  t === "up"
    ? `<span style="color:${C.GREEN};font-size:16px;">&#9650;</span>`
    : t === "down"
      ? `<span style="color:${C.RED};font-size:16px;">&#9660;</span>`
      : t === "new"
        ? `<span style="color:${C.ACCENT_BLUE};font-size:12px;font-weight:bold;">NEW</span>`
        : `<span style="color:${C.AMBER};font-size:16px;">&#9679;</span>`;

function deltaGlyph(cur: number, prev: number | null): string {
  if (prev == null) return `<span style="color:${C.TEXT_GRAY};">&#8212;</span>`;
  const d = cur - prev;
  if (d > 0.05) return `<span style="color:${C.GREEN};">&#9650;</span>`;
  if (d < -0.05) return `<span style="color:${C.RED};">&#9660;</span>`;
  return `<span style="color:${C.AMBER};">&#9679;</span>`;
}

function fmtDate(iso: string, tz = "America/Mexico_City"): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return new Intl.DateTimeFormat("en-US", { timeZone: tz, month: "short", day: "numeric" }).format(d);
}
function fmtTime(iso: string, tz = "America/Mexico_City"): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return new Intl.DateTimeFormat("en-US", { timeZone: tz, hour: "2-digit", minute: "2-digit", hour12: false }).format(d);
}

const cardHead = (title: string) =>
  `<tr><td style="background:${C.DARK_NAVY};color:${C.WHITE};padding:12px 16px;font-size:16px;font-weight:bold;font-family:Arial,sans-serif;">${esc(title)}</td></tr>`;
const cardOpen = () =>
  `<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border:1px solid ${C.BORDER};border-radius:8px;overflow:hidden;margin-bottom:16px;">`;
const th = (label: string, align = "left", extra = "") =>
  `<th style="text-align:${align};padding:6px 8px;color:${C.TEXT_GRAY};font-weight:bold;font-size:11px;text-transform:uppercase;${extra}">${label}</th>`;

export function renderDigestEmail(
  facts: DigestFacts,
  summary: DigestSummary | null,
  opts: { baseUrl?: string; timezone?: string } = {}
): { subject: string; html: string; text: string } {
  const base = opts.baseUrl ?? BASE_URL;
  const tz = opts.timezone ?? "America/Mexico_City";
  const dateLabel = new Intl.DateTimeFormat("en-US", {
    timeZone: "UTC",
    month: "long",
    day: "numeric",
    year: "numeric",
  }).format(new Date(`${facts.pull_date}T12:00:00Z`));
  const first = facts.agent.name.split(/\s+/)[0] || facts.agent.name;
  const subject = `Nightly QA Digest — ${facts.agent.name} — ${facts.pull_date}`;
  const noteBy = new Map((summary?.sections ?? []).map((s) => [s.section_id, s]));

  // Tonight vs month table
  let rows = "";
  for (const s of facts.sections) {
    const t = fmtStat(s.type, facts.tonight.section_stats[s.id]);
    const m = fmtStat(s.type, facts.month.section_stats[s.id]);
    const n = noteBy.get(s.id);
    rows +=
      `<tr style="border-bottom:1px solid #F0F0F0;">` +
      `<td style="padding:8px;color:${C.DARK_NAVY};font-weight:bold;vertical-align:top;">${esc(s.name)}</td>` +
      `<td style="padding:8px;text-align:center;font-weight:bold;color:${t.color};vertical-align:top;">${t.text}</td>` +
      `<td style="padding:8px;text-align:center;color:${m.color};vertical-align:top;">${m.text}</td>` +
      `<td style="padding:8px;text-align:center;vertical-align:top;">${n ? trendGlyph(n.trend) : ""}</td>` +
      `<td style="padding:8px;color:${C.TEXT_GRAY};font-size:12px;line-height:1.4;">${n ? esc(n.note) : ""}</td>` +
      `</tr>`;
  }
  const tonightTable =
    cardOpen() +
    cardHead(`Tonight — ${facts.tonight.n} call${facts.tonight.n === 1 ? "" : "s"} scored`) +
    `<tr><td style="padding:16px;font-family:Arial,sans-serif;">` +
    `<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="font-size:13px;">` +
    `<tr style="border-bottom:2px solid ${C.LIGHT_BLUE};">${th("Section")}${th("Tonight", "center")}${th(`${facts.month.label} avg`, "center")}${th("Trend", "center")}${th("What moved", "left", "width:44%;")}</tr>` +
    rows +
    `</table>` +
    (summary
      ? ""
      : `<p style="font-size:12px;color:${C.TEXT_GRAY};margin:12px 0 0 0;">Trend summary unavailable tonight — the figures above are complete.</p>`) +
    `</td></tr></table>`;

  // Headline + focus
  const headline = summary
    ? `<p style="font-size:15px;color:${C.DARK_NAVY};line-height:1.5;margin:0 0 16px 0;">${esc(summary.headline)}</p>`
    : `<p style="font-size:15px;color:${C.DARK_NAVY};line-height:1.5;margin:0 0 16px 0;">Hi ${esc(first)}, here is how tonight's QA went` +
      (facts.tonight.avg != null ? ` — overall <b style="color:${overallColor(facts.tonight.avg)};">${facts.tonight.avg.toFixed(1)}</b>` : "") +
      (facts.month.avg != null ? `, month to date <b>${facts.month.avg.toFixed(1)}</b>` : "") +
      `.</p>`;
  const focus = summary
    ? cardOpen() +
      cardHead("Focus for your next shift") +
      `<tr><td style="padding:16px;font-family:Arial,sans-serif;font-size:14px;color:${C.TEXT_GRAY};line-height:1.55;">${esc(summary.focus).replace(/\n/g, "<br>")}</td></tr></table>`
    : "";

  // Progression card (ProgressionCard.js contract: oldest → newest, newest highlighted)
  let prog = "";
  const entries = facts.recent;
  for (let i = 0; i < entries.length; i++) {
    const e = entries[i];
    const prev = i > 0 ? entries[i - 1].overall : null;
    const color = overallColor(e.overall);
    const pct = Math.min(e.overall, 100).toFixed(0);
    const rowBg = e.tonight ? C.LIGHT_BLUE : C.WHITE;
    prog +=
      `<tr style="background:${rowBg};${i < entries.length - 1 ? "border-bottom:1px solid #F0F0F0;" : ""}">` +
      `<td style="padding:8px;color:${C.DARK_NAVY};${e.tonight ? "font-weight:bold;" : ""}">${fmtDate(e.ts, tz)}${e.tonight ? " &#9668;" : ""}</td>` +
      `<td style="padding:8px;text-align:center;font-weight:bold;color:${color};">${e.overall.toFixed(1)}</td>` +
      `<td style="padding:8px;text-align:center;font-size:16px;">${deltaGlyph(e.overall, prev)}</td>` +
      `<td style="padding:8px;"><table role="presentation" cellpadding="0" cellspacing="0" width="100%"><tr>` +
      `<td style="background:#EEEEEE;border-radius:4px;height:10px;"><table role="presentation" cellpadding="0" cellspacing="0" width="${pct}%"><tr><td style="background:${color};border-radius:4px;height:10px;font-size:1px;line-height:10px;">&nbsp;</td></tr></table></td>` +
      `</tr></table></td></tr>`;
  }
  const monthLine =
    facts.month.avg != null
      ? `<p style="font-size:14px;color:${C.DARK_NAVY};margin:12px 0 0 0;">Month to date (${esc(facts.month.label)}): <b style="color:${overallColor(facts.month.avg)};font-size:16px;">${facts.month.avg.toFixed(1)}</b> over ${facts.month.n} evaluation${facts.month.n === 1 ? "" : "s"}. This is your plain average for the month — the number your monthly QA goal is measured on.</p>`
      : "";
  const progression =
    cardOpen() +
    cardHead("QA Progression") +
    `<tr><td style="padding:16px;font-family:Arial,sans-serif;">` +
    (entries.length
      ? `<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="font-size:13px;">` +
        `<tr style="border-bottom:2px solid ${C.LIGHT_BLUE};">${th("Date")}${th("Score", "center")}${th("Trend", "center")}${th("Progress", "left", "width:40%;")}</tr>` +
        prog +
        `</table>`
      : "") +
    monthLine +
    `</td></tr></table>`;

  // Per-call strip
  let calls = "";
  for (const e of facts.tonight.evals) {
    calls +=
      `<tr style="border-bottom:1px solid #F0F0F0;">` +
      `<td style="padding:8px;color:${C.DARK_NAVY};">${fmtDate(e.ts, tz)} ${fmtTime(e.ts, tz)}</td>` +
      `<td style="padding:8px;color:${C.TEXT_GRAY};font-size:12px;">${esc(e.disposition || "—")}</td>` +
      `<td style="padding:8px;text-align:center;font-weight:bold;color:${overallColor(e.overall)};">${e.overall.toFixed(1)}</td>` +
      `<td style="padding:8px;text-align:right;"><a href="${base}/datapoint/${encodeURIComponent(facts.team_id)}/${encodeURIComponent(e.call_id ?? String(e.eval_id))}" style="color:${C.ACCENT_BLUE};font-size:12px;">full scorecard</a></td>` +
      `</tr>`;
  }
  const pending = facts.pending_review
    ? `<p style="font-size:12px;color:${C.TEXT_GRAY};margin:12px 0 0 0;">${facts.pending_review} more call${facts.pending_review === 1 ? " is" : "s are"} with a QA analyst for review — you'll get that scorecard separately once it's approved.</p>`
    : "";
  const callsCard =
    cardOpen() +
    cardHead("Tonight's calls") +
    `<tr><td style="padding:16px;font-family:Arial,sans-serif;">` +
    `<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="font-size:13px;">` +
    `<tr style="border-bottom:2px solid ${C.LIGHT_BLUE};">${th("When")}${th("Disposition")}${th("Score", "center")}${th("", "right")}</tr>` +
    calls +
    `</table>${pending}</td></tr></table>`;

  const html =
    `<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:${C.BG};padding:24px 0;"><tr><td align="center">` +
    `<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="background:${C.WHITE};border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.08);">` +
    `<tr><td style="background:${C.DARK_NAVY};padding:20px 24px;font-family:Arial,sans-serif;">` +
    `<div style="color:${C.WHITE};font-size:20px;font-weight:bold;">Nightly QA Digest</div>` +
    `<div style="color:${C.LIGHT_BLUE};font-size:13px;margin-top:4px;">${esc(facts.agent.name)} &middot; ${esc(dateLabel)}</div>` +
    `</td></tr>` +
    `<tr><td style="padding:24px;font-family:Arial,sans-serif;">` +
    headline +
    tonightTable +
    focus +
    progression +
    callsCard +
    `<p style="font-size:11px;color:#9A9A9A;margin:8px 0 0 0;line-height:1.5;">Scores come from the AI evaluator against the current Member Support rubric (${esc(facts.rubric_version)}); Process Adherence is scored only against the SOP documents retrieved for each call. Questions about a score? Reply to your QA analyst.</p>` +
    `</td></tr></table></td></tr></table>`;

  const textLines = [
    `Nightly QA Digest — ${facts.agent.name} — ${facts.pull_date}`,
    summary ? summary.headline : `Tonight: ${facts.tonight.n} calls scored${facts.tonight.avg != null ? `, overall ${facts.tonight.avg.toFixed(1)}` : ""}.`,
    "",
    ...facts.sections.map((s) => {
      const t = fmtStat(s.type, facts.tonight.section_stats[s.id]).text;
      const m = fmtStat(s.type, facts.month.section_stats[s.id]).text;
      const n = noteBy.get(s.id);
      return `${s.name}: tonight ${t}, month ${m}${n ? ` [${n.trend}] ${n.note}` : ""}`;
    }),
    "",
    summary ? `Focus: ${summary.focus}` : "",
    facts.month.avg != null ? `Month to date (${facts.month.label}): ${facts.month.avg.toFixed(1)} over ${facts.month.n} evaluations.` : "",
  ];
  return { subject, html, text: textLines.filter((l) => l !== undefined).join("\n") };
}

// ── transport: GAS {html_email} branch (qa-automation/src/Main.js) ────────

export async function sendHtmlEmail(
  gasUrl: string | undefined,
  msg: { to: string; cc?: string; subject: string; html: string; text: string },
  fetchImpl: typeof fetch = fetch
): Promise<{ status: string; message: string }> {
  if (!gasUrl) return { status: "skipped", message: "GAS_WEBAPP_URL_MS app secret not configured" };
  if (!msg.to) return { status: "skipped", message: "no recipient" };
  try {
    const res = await fetchImpl(gasUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ html_email: msg }),
      redirect: "follow",
      signal: AbortSignal.timeout(60_000),
    });
    const text = await res.text();
    try {
      const parsed = JSON.parse(text);
      if (parsed && typeof parsed.status === "string") return parsed;
      return { status: "error", message: `unexpected GAS response: ${text.slice(0, 200)}` };
    } catch {
      return { status: "error", message: `GAS webapp returned non-JSON (HTTP ${res.status}): ${text.slice(0, 200)}` };
    }
  } catch (err) {
    return { status: "error", message: String((err as any)?.message ?? err).slice(0, 300) };
  }
}

// ── summary persist (qa-insights callback, ref.kind = 'daily_digest') ─────

export async function persistDigestSummary(db: D1Database, ref: any, item: any): Promise<void> {
  const row = await db
    .prepare("SELECT id, facts FROM qa_agent_digests WHERE id = ?")
    .bind(Number(ref.digest_id))
    .first<any>();
  if (!row) throw new Error(`digest row ${ref.digest_id} not found`);
  const now = new Date().toISOString();
  try {
    if (!item.ok) throw new Error(item.error ?? "item failed");
    const summary = parseDigestSummary(parseJson(row.facts, null), item.text ?? "");
    await db
      .prepare(
        `UPDATE qa_agent_digests SET summary = ?, summary_model = ?, summary_usage = ?, summary_error = NULL, updated_at = ?
         WHERE id = ?`
      )
      .bind(JSON.stringify(summary), item.model ?? null, item.usage ? JSON.stringify(item.usage) : null, now, row.id)
      .run();
  } catch (err) {
    await db
      .prepare("UPDATE qa_agent_digests SET summary_error = ?, summary_model = ?, updated_at = ? WHERE id = ?")
      .bind(String((err as any)?.message ?? err).slice(0, 300), item.model ?? null, now, row.id)
      .run();
  }
}

// ── cron job handler: job='daily_digest', key='<team>:<pull_date>' ────────

export interface DigestJobDeps {
  now?: () => number;
  triggerInsights?: (db: D1Database, request: Request, payload: any) => Promise<{ ok: true; runId: string | null } | { ok: false; detail: string }>;
  fetchImpl?: typeof fetch;
}

function pastDeadline(nowMs: number, deadlineUtc: number): boolean {
  const d = new Date(nowMs);
  return d.getUTCHours() + d.getUTCMinutes() / 60 >= deadlineUtc;
}

export function makeDailyDigestHandler(deps: DigestJobDeps = {}): JobHandler {
  const now = deps.now ?? (() => Date.now());
  return async (db, request, env, row, report) => {
    const [teamId, pullDate] = row.key.split(":");
    const out = { ...(report ?? {}) };
    const team = await db.prepare("SELECT provider_config FROM teams WHERE id = ?").bind(teamId).first<any>();
    const cfg = digestConfigFor(team?.provider_config) ?? {};
    const phase = row.phase ?? "await_scores";
    const nowMs = now();

    if (phase === "await_scores") {
      const pull = await db
        .prepare("SELECT report FROM qa_disposition_pulls WHERE team_id = ? AND pull_date = ?")
        .bind(teamId, pullDate)
        .first<any>();
      const picks: string[] = parseJson(pull?.report, {})?.picked_call_ids ?? [];
      out.picks = picks.length;
      if (!picks.length) return { done: true, phase: "done", report: { ...out, note: "no picks recorded for this pull" } };
      let pending = 0;
      for (const ids of chunk(picks, ID_CHUNK)) {
        const r = await db
          .prepare(
            `SELECT COUNT(*) AS n FROM qa_score_queue
             WHERE team_id = ? AND status IN ('queued','triggering','running')
               AND call_id IN (${ids.map(() => "?").join(",")})`
          )
          .bind(teamId, ...ids)
          .first<any>();
        pending += Number(r?.n ?? 0);
      }
      out.pending_scores = pending;
      if (pending > 0 && !pastDeadline(nowMs, cfg.send_deadline_utc ?? DEFAULT_DEADLINE_UTC))
        return { done: false, phase: "await_scores", report: out, wait_s: AWAIT_SCORES_WAIT_S };
      if (pending > 0) out.deadline_hit = true;
      return { done: false, phase: "summarize", cursor: 0, report: out };
    }

    if (phase === "summarize") {
      const cursor = Number(row.cursor ?? 0);
      if (cursor === 0 && !out.rows_built) {
        const { loadTeamConfig } = await import("./teamConfig.js");
        const config = await loadTeamConfig(db, teamId);
        const pull = await db
          .prepare("SELECT report FROM qa_disposition_pulls WHERE team_id = ? AND pull_date = ?")
          .bind(teamId, pullDate)
          .first<any>();
        const picks: string[] = parseJson(pull?.report, {})?.picked_call_ids ?? [];
        const { finalized, pendingReviewByAgent } = await nightCohort(db, teamId, picks);
        const byAgent = new Map<number, any[]>();
        for (const ev of finalized) {
          const list = byAgent.get(Number(ev.agent_id)) ?? [];
          list.push(ev);
          byAgent.set(Number(ev.agent_id), list);
        }
        const roster = (
          await db
            .prepare("SELECT id, name, canonical_name, email, supervisor_email FROM qa_agents WHERE team_id = ? AND active = 1")
            .bind(teamId)
            .all<any>()
        ).results;
        let built = 0;
        for (const a of roster) {
          const evs = byAgent.get(Number(a.id));
          if (!evs?.length) continue;
          const agent = { id: Number(a.id), name: a.canonical_name || a.name, email: String(a.email ?? "").toLowerCase() };
          const facts = await buildAgentFacts(db, config, teamId, pullDate, agent, evs, pendingReviewByAgent[agent.id] ?? 0, nowMs);
          await db
            .prepare(
              `INSERT INTO qa_agent_digests (team_id, pull_date, agent_id, agent_email, agent_name, eval_ids, facts)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(team_id, pull_date, agent_id) DO UPDATE SET
                 eval_ids = excluded.eval_ids, facts = excluded.facts,
                 updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')`
            )
            .bind(teamId, pullDate, agent.id, agent.email, agent.name, JSON.stringify(evs.map((e) => Number(e.id))), JSON.stringify(facts))
            .run();
          built++;
        }
        out.rows_built = built;
        out.finalized_evals = finalized.length;
        out.agents_inactive_or_unmatched = [...byAgent.keys()].filter((id) => !roster.some((a) => Number(a.id) === id)).length;
        if (!built) return { done: true, phase: "done", report: { ...out, note: "no finalized evals for active agents" } };
      }
      // One qa-insights run per chunk of ≤40 agents (cursor = chunk index).
      const needing = (
        await db
          .prepare(
            `SELECT id, facts FROM qa_agent_digests
             WHERE team_id = ? AND pull_date = ? AND summary IS NULL AND summary_error IS NULL AND email_status = 'pending'
             ORDER BY id`
          )
          .bind(teamId, pullDate)
          .all<any>()
      ).results;
      const chunks = chunk(needing, INSIGHTS_MAX_ITEMS);
      if (cursor >= chunks.length) {
        return { done: false, phase: "await_summary", cursor: 0, report: { ...out, summary_started_at: new Date(nowMs).toISOString() } };
      }
      const items = chunks[cursor].map((r) => {
        const facts = parseJson(r.facts, null) as DigestFacts;
        const { system, prompt } = buildDigestPrompts(facts);
        return { ref: { kind: "daily_digest", digest_id: r.id, team_id: teamId, pull_date: pullDate, agent_id: facts.agent.id }, system, prompt };
      });
      const trigger = deps.triggerInsights ?? (await import("../routes/insights.js")).triggerInsights;
      const res = await trigger(db, request, {
        mode: "daily_digest",
        model: { model: cfg.model ?? DEFAULT_MODEL, max_tokens: cfg.max_tokens ?? DEFAULT_MAX_TOKENS },
        items,
      });
      const runs = out.summary_runs ?? [];
      if (res.ok) {
        runs.push({ chunk: cursor, items: items.length, run_id: res.runId });
        out.summary_runs = runs;
        return { done: false, phase: "summarize", cursor: cursor + 1, report: out };
      }
      // Engine busy (409) or unreachable: retry this chunk after a pause; the
      // await_summary timeout still bounds the night.
      runs.push({ chunk: cursor, items: items.length, error: res.detail });
      out.summary_runs = runs;
      out.summary_trigger_failures = (out.summary_trigger_failures ?? 0) + 1;
      if (out.summary_trigger_failures >= 5) {
        return { done: false, phase: "await_summary", cursor: 0, report: { ...out, summary_started_at: new Date(nowMs).toISOString() } };
      }
      return { done: false, phase: "summarize", cursor, report: out, wait_s: AWAIT_SUMMARY_WAIT_S };
    }

    if (phase === "await_summary") {
      const r = await db
        .prepare(
          `SELECT COUNT(*) AS n FROM qa_agent_digests
           WHERE team_id = ? AND pull_date = ? AND summary IS NULL AND summary_error IS NULL AND email_status = 'pending'`
        )
        .bind(teamId, pullDate)
        .first<any>();
      const waiting = Number(r?.n ?? 0);
      const startedMs = Date.parse(out.summary_started_at ?? new Date(nowMs).toISOString());
      const timeoutMs = (cfg.summary_timeout_min ?? DEFAULT_SUMMARY_TIMEOUT_MIN) * 60_000;
      if (waiting > 0 && nowMs - startedMs < timeoutMs)
        return { done: false, phase: "await_summary", report: { ...out, summaries_waiting: waiting }, wait_s: AWAIT_SUMMARY_WAIT_S };
      if (waiting > 0) {
        await db
          .prepare(
            `UPDATE qa_agent_digests SET summary_error = 'timeout: no summary within the send window', updated_at = ?
             WHERE team_id = ? AND pull_date = ? AND summary IS NULL AND summary_error IS NULL AND email_status = 'pending'`
          )
          .bind(new Date(nowMs).toISOString(), teamId, pullDate)
          .run();
        out.summaries_timed_out = waiting;
      }
      return { done: false, phase: "send", cursor: 0, report: out };
    }

    if (phase === "send") {
      const next = await db
        .prepare(
          `SELECT d.*, a.supervisor_email FROM qa_agent_digests d
           LEFT JOIN qa_agents a ON a.id = d.agent_id
           WHERE d.team_id = ? AND d.pull_date = ? AND d.email_status = 'pending' ORDER BY d.id LIMIT 1`
        )
        .bind(teamId, pullDate)
        .first<any>();
      if (!next) return { done: true, phase: "done", report: out };
      const facts = parseJson(next.facts, null) as DigestFacts;
      const summary = parseJson(next.summary, null) as DigestSummary | null;
      const mail = renderDigestEmail(facts, summary, { timezone: parseJson(team?.provider_config, {})?.nightly_sweep?.timezone });
      const { gasUrlForTeam } = await import("./emailDispatch.js");
      const gasUrl = gasUrlForTeam(
        { member_support: env.GAS_WEBAPP_URL_MS, sales: env.GAS_WEBAPP_URL_SALES, sofia: env.GAS_WEBAPP_URL_SOFIA },
        teamId
      );
      const receipt = await sendHtmlEmail(
        gasUrl,
        {
          to: next.agent_email,
          cc: cfg.cc_supervisor && next.supervisor_email ? String(next.supervisor_email) : undefined,
          subject: mail.subject,
          html: mail.html,
          text: mail.text,
        },
        deps.fetchImpl
      );
      const status = ["ok", "skipped", "error"].includes(receipt.status) ? receipt.status : "error";
      await db
        .prepare(
          `UPDATE qa_agent_digests SET email_status = ?, email_message = ?, sent_at = ?, updated_at = ? WHERE id = ?`
        )
        .bind(status, String(receipt.message ?? "").slice(0, 300), status === "ok" ? new Date(nowMs).toISOString() : null, new Date(nowMs).toISOString(), next.id)
        .run();
      out.sent = { ...(out.sent ?? {}), [status]: ((out.sent ?? {})[status] ?? 0) + 1 };
      return { done: false, phase: "send", cursor: Number(row.cursor ?? 0) + 1, report: out };
    }

    return { done: true, phase: "done", report: { ...out, error: `unknown phase ${phase}` } };
  };
}

export const dailyDigestJob: JobHandler = makeDailyDigestHandler();

// Agent one-pager — port of backend/services/onepager.py (JulyR2R3 §3).
// Renders the print-ready monthly HTML from the SAME analytics frame the
// dashboards use, plus the PERSISTED month assessment (never generates —
// a page view must never spend a Gemini call).

import type { FrameRow } from "./historyFrame.js";
import type { TeamConfig } from "./teamConfig.js";
import { monthInBucketTz, BUCKET_TZ } from "./teamStats.js";
import { evalWindowStat } from "./coachingFacts.js";

const NAVY = "#15192D";
const BLUE = "#1A61D9";
const LIGHT_BLUE = "#E7EFFB";
const GREEN = "#28A745";
const AMBER = "#E8A317";
const RED = "#D9534F";
const GRAY = "#4A4A4A";

const esc = (s: any) =>
  String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

export function lastClosedMonth(now = new Date()): string {
  const ym = monthInBucketTz(now.getTime());
  const [y, m] = ym.split("-").map(Number);
  return m === 1 ? `${y - 1}-12` : `${y}-${String(m - 1).padStart(2, "0")}`;
}

const fmtG = (n: number) => String(parseFloat(n.toFixed(10))); // Python %g-ish

interface SectionStat {
  name: string;
  kind: "binary" | "numeric";
  avg: number | null;
  delta: number | null;
  na: number;
}

function mean(xs: number[]): number {
  return xs.reduce((a, b) => a + b, 0) / xs.length;
}

function sectionStat(
  name: string,
  values: (string | number | null)[]
): SectionStat {
  const strs = values.map((v) => String(v ?? "").trim());
  const isBinary = strs.some((v) => ["Yes", "Y", "No", "N"].includes(v));
  const na = strs.filter((v) => v === "Not Applicable" || v === "NA").length;
  const midpoint = Math.floor(values.length / 2);
  if (isBinary) {
    const mapped = strs
      .map((v) => (["Yes", "Y"].includes(v) ? 1 : ["No", "N"].includes(v) ? 0 : null))
      .filter((v): v is number => v !== null);
    const first = strs.slice(0, midpoint), second = strs.slice(midpoint);
    const mapHalf = (half: string[]) =>
      half
        .map((v) => (["Yes", "Y"].includes(v) ? 1 : ["No", "N"].includes(v) ? 0 : null))
        .filter((v): v is number => v !== null);
    const f = mapHalf(first), s = mapHalf(second);
    return {
      name, kind: "binary", na,
      avg: mapped.length ? Math.round(mean(mapped) * 1000) / 10 : null,
      delta: f.length && s.length ? Math.round((mean(s) - mean(f)) * 1000) / 10 : null,
    };
  }
  const nums = values
    .map((v) => (typeof v === "number" ? v : parseFloat(String(v))))
    .filter((v) => Number.isFinite(v));
  const first = nums.slice(0, Math.floor(nums.length / 2));
  const second = nums.slice(Math.floor(nums.length / 2));
  return {
    name, kind: "numeric", na,
    avg: nums.length ? Math.round(mean(nums) * 100) / 100 : null,
    delta: first.length && second.length ? Math.round((mean(second) - mean(first)) * 100) / 100 : null,
  };
}

function trendArrow(delta: number | null): [string, string] {
  if (delta === null) return ["—", GRAY];
  if (delta > 0.05) return [`▲ +${fmtG(delta)}`, GREEN];
  if (delta < -0.05) return [`▼ ${fmtG(delta)}`, RED];
  return ["→ steady", GRAY];
}

function sparkline(scores: number[], width = 560, height = 64): string {
  if (scores.length < 2) return "";
  const lo = Math.min(...scores, 60), hi = Math.max(...scores, 100);
  const span = hi - lo || 1;
  const step = width / (scores.length - 1);
  const points = scores.map((s, i) => [
    Math.round(i * step * 10) / 10,
    Math.round((height - ((s - lo) / span) * (height - 8) - 4) * 10) / 10,
  ]);
  const polyline = points.map(([x, y]) => `${x},${y}`).join(" ");
  const dots = points
    .map(([x, y]) => `<circle cx="${x}" cy="${y}" r="2.5" fill="${BLUE}"/>`)
    .join("");
  return (
    `<svg width="${width}" height="${height}" viewBox="0 0 ${width} ${height}">` +
    `<polyline points="${polyline}" fill="none" stroke="${BLUE}" stroke-width="2"/>${dots}</svg>`
  );
}

function bar(stat: SectionStat): string {
  if (stat.avg === null) return "";
  const pct = stat.kind === "binary" ? stat.avg : (stat.avg / 5) * 100;
  const color = pct >= 80 ? GREEN : pct >= 60 ? AMBER : RED;
  return `<div class="bar"><div class="fill" style="width:${Math.round(pct)}%;background:${color}"></div></div>`;
}

function assessmentHtml(assessment: any | null): string {
  if (!assessment) {
    return (
      '<div class="assessment"><strong>AI Assessment</strong><br>' +
      '<span class="muted">No persisted assessment for this window — ' +
      "the monthly export run creates one.</span></div>"
    );
  }
  const lines = (assessment.sections ?? [])
    .map((s: any) => {
      const [arrow, color] =
        s.trend === "improving" ? ["▲", GREEN] : s.trend === "declining" ? ["▼", RED] : ["→", GRAY];
      return (
        `<div class="a-sec"><span style="color:${color}">${arrow}</span> ` +
        `<strong>${esc(s.section_name)}</strong> — ${esc(s.coaching_tip)}</div>`
      );
    })
    .join("");
  const stamp = (assessment.generated_at ?? "").slice(0, 10);
  return (
    '<div class="assessment"><strong>AI Assessment</strong> ' +
    `<span class="muted">(${assessment.evaluations_included ?? "?"} evaluations` +
    `${stamp ? " · " + stamp : ""} · ${esc(assessment.rubric_version ?? "")})</span>` +
    `<p style="margin:6px 0 8px">${esc(assessment.overall_assessment)}</p>${lines}</div>`
  );
}

const MONTHS = ["January","February","March","April","May","June","July","August","September","October","November","December"];

// ── Coaching & commitments block (CoachingLoopSpec §7, CL4) ────────────────
// Sessions whose conduct date OR deadline lands in the month, with
// commitment statuses and the deterministic response readout. Renders
// nothing for months without coaching (layout unchanged — additive, like
// the assessment box).

const COMMIT_GLYPHS: Record<string, string> = {
  open: "○", met: "✓", partially_met: "◐", not_met: "✗", waived: "–",
};
const OUTCOME_COLORS: Record<string, string> = {
  met: GREEN, partially_met: AMBER, not_met: RED,
};

async function coachingBlock(
  db: D1Database,
  config: TeamConfig,
  agent: string,
  month: string
): Promise<string> {
  const roster = await db
    .prepare(
      `SELECT id FROM qa_agents WHERE team_id = ?
         AND (name = ? OR canonical_name = ?) LIMIT 1`
    )
    .bind(config.team_id, agent, agent)
    .first<any>();
  if (!roster) return "";
  const sessions = (
    await db
      .prepare(
        `SELECT * FROM qa_coachings
         WHERE team_id = ? AND agent_id = ? AND status != 'cancelled'
           AND (substr(COALESCE(completed_at, ''), 1, 7) = ?
                OR substr(COALESCE(action_plan_deadline, ''), 1, 7) = ?)
         ORDER BY COALESCE(completed_at, created_at)`
      )
      .bind(config.team_id, roster.id, month, month)
      .all<any>()
  ).results;
  if (!sessions.length) return "";

  const parts: string[] = [];
  for (const s of sessions) {
    const commits = (
      await db
        .prepare(
          "SELECT commitment, status FROM qa_coaching_commitments WHERE coaching_id = ? ORDER BY id"
        )
        .bind(s.id)
        .all<any>()
    ).results;
    let readout = "";
    if (s.completed_at) {
      const preStart = new Date(Date.parse(s.completed_at) - 30 * 86_400_000).toISOString();
      const postEnd = s.action_plan_deadline
        ? `${s.action_plan_deadline}T23:59:59Z`
        : new Date().toISOString();
      const pre = await evalWindowStat(db, config.team_id, roster.id, preStart, s.completed_at);
      const post = await evalWindowStat(db, config.team_id, roster.id, s.completed_at, postEnd);
      if (pre.n && post.n)
        readout =
          `<div class="c-readout">Response: ${pre.n} calls avg ${pre.avg} before → ` +
          `${post.n} calls avg ${post.avg} after coaching</div>`;
    }
    const outcomeBadge = s.outcome
      ? `<span class="c-outcome" style="background:${OUTCOME_COLORS[s.outcome] ?? GRAY}">` +
        `${esc(s.outcome.replace(/_/g, " "))}</span>`
      : `<span class="c-outcome" style="background:${GRAY}">confirmation due ${esc(s.action_plan_deadline ?? "—")}</span>`;
    const commitLis = commits
      .map(
        (k: any) =>
          `<div class="c-commit"><span style="color:${OUTCOME_COLORS[k.status] ?? GRAY}">` +
          `${COMMIT_GLYPHS[k.status] ?? "○"}</span> ${esc(k.commitment)}</div>`
      )
      .join("");
    parts.push(
      `<div class="c-sess">` +
        `<div><strong>${esc((s.completed_at ?? s.created_at ?? "").slice(0, 10))}</strong> ` +
        `· ${esc(s.conducted_by_role)} ${outcomeBadge}` +
        (s.action_plan_deadline ? ` <span class="muted">deadline ${esc(s.action_plan_deadline)}</span>` : "") +
        `</div>${commitLis}${readout}</div>`
    );
  }
  return (
    `<h2>Coaching &amp; commitments</h2><div class="coach-block">` +
    parts.join("") +
    `</div>`
  );
}

export async function renderMonthOnepager(
  db: D1Database,
  config: TeamConfig,
  frame: FrameRow[],
  agent: string,
  month: string,
  teamLabel: string
): Promise<string | null> {
  const rows = frame
    .filter((r) => r.agent === agent && monthInBucketTz(r.ts) === month)
    .sort((a, b) => a.ts - b.ts);
  if (!rows.length) return null;

  const overall = rows.map((r) => r.overall_score);
  const n = overall.length;
  const m = mean(overall);
  const std =
    n > 1 ? Math.sqrt(overall.reduce((a, b) => a + (b - m) ** 2, 0) / (n - 1)) : 0;
  const [yy, mm] = month.split("-").map(Number);
  const monthLabel = `${MONTHS[mm - 1]} ${yy}`;

  const stats: SectionStat[] = [];
  for (const sec of config.sections_by_number) {
    const h = sec.history_id;
    const vals = rows.map((r) =>
      h in r.num ? r.num[h] : h in r.yn ? r.yn[h] : null
    );
    if (vals.every((v) => v === null)) continue;
    stats.push(sectionStat(sec.name, vals));
  }

  // Persisted month assessment: the is_current row whose window intersects
  // the month, newest first (deviation note: Railway resolves via
  // assessment_store's month-window key; interval-intersection is the
  // D1-side equivalent for the read path).
  const monthStart = `${month}-01T00:00:00Z`;
  const monthEnd = `${month}-31T23:59:59Z`;
  const arow = await db
    .prepare(
      `SELECT a.*, ag.name AS agent_name FROM qa_assessments a
       JOIN qa_agents ag ON ag.id = a.agent_id
       WHERE a.team_id = ? AND a.is_current = 1
         AND (ag.name = ? OR ag.canonical_name = ?)
         AND a.range_start_at <= ? AND a.range_end_at >= ?
       ORDER BY a.generated_at DESC LIMIT 1`
    )
    .bind(config.team_id, agent, agent, monthEnd, monthStart)
    .first<any>();
  let assessment: any = null;
  if (arow) {
    const secs = await db
      .prepare(
        "SELECT section_name, trend, coaching_tip FROM qa_assessment_sections WHERE assessment_id = ? ORDER BY section_number"
      )
      .bind(arow.id)
      .all<any>();
    assessment = { ...arow, sections: secs.results };
  }

  const tableRows = stats
    .map((s) => {
      const [arrow, color] = trendArrow(s.delta);
      const avgCell =
        s.avg === null ? "—" : s.kind === "binary" ? `${fmtG(s.avg)}% Yes` : `${fmtG(s.avg)} / 5`;
      const naCell = s.na ? `${s.na} NA` : "";
      return (
        `<tr><td>${esc(s.name)}</td><td class="num">${avgCell}</td>` +
        `<td>${bar(s)}</td><td class="num" style="color:${color}">${arrow}</td>` +
        `<td class="num muted">${naCell}</td></tr>`
      );
    })
    .join("\n");

  const genStamp = new Intl.DateTimeFormat("sv-SE", {
    timeZone: BUCKET_TZ, dateStyle: "short", timeStyle: "short",
  }).format(new Date());

  const coachingHtml = await coachingBlock(db, config, agent, month);

  return `<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>${esc(agent)} — QA ${monthLabel}</title>
<style>
  @page { size: letter portrait; margin: 0.55in; }
  * { box-sizing: border-box; margin: 0; }
  body { font: 13px/1.45 -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;
         color: ${GRAY}; max-width: 7.4in; margin: 0 auto; padding: 24px 8px; }
  header { background: ${NAVY}; color: #fff; border-radius: 10px;
            padding: 18px 22px; display: flex; justify-content: space-between;
            align-items: baseline; }
  header h1 { font-size: 21px; font-weight: 650; }
  header .brand { color: ${LIGHT_BLUE}; font-size: 12px; letter-spacing: 0.12em;
                   text-transform: uppercase; }
  .cards { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px;
            margin: 14px 0; }
  .card { background: ${LIGHT_BLUE}; border-radius: 8px; padding: 12px 14px; }
  .card .v { font-size: 26px; font-weight: 700; color: ${NAVY};
              font-variant-numeric: tabular-nums; }
  .card .l { font-size: 10.5px; text-transform: uppercase; letter-spacing: 0.08em; }
  h2 { font-size: 12px; color: ${NAVY}; text-transform: uppercase;
        letter-spacing: 0.1em; margin: 18px 0 8px; }
  table { width: 100%; border-collapse: collapse; }
  td { padding: 5px 8px; border-bottom: 1px solid #eceff4; }
  td.num { text-align: right; white-space: nowrap;
            font-variant-numeric: tabular-nums; }
  .muted { color: #9aa3af; font-size: 11px; }
  .bar { width: 130px; height: 7px; background: #e8ebf0; border-radius: 4px; }
  .fill { height: 100%; border-radius: 4px; }
  .assessment { border: 1.5px dashed ${BLUE}; border-radius: 10px; padding: 14px 16px;
                 margin-top: 16px; color: ${NAVY}; background: #fbfcff; }
  .a-sec { font-size: 11.5px; margin: 2px 0; }
  .coach-block { border: 1px solid #e8ebf0; border-radius: 10px; padding: 12px 14px; }
  .c-sess { margin-bottom: 10px; font-size: 12px; }
  .c-sess:last-child { margin-bottom: 0; }
  .c-outcome { color: #fff; font-size: 10px; padding: 1px 8px; border-radius: 8px;
               text-transform: uppercase; letter-spacing: 0.05em; }
  .c-commit { font-size: 11.5px; margin: 3px 0 0 8px; }
  .c-readout { font-size: 11px; color: #9aa3af; margin-top: 4px; }
  footer { margin-top: 18px; font-size: 10.5px; color: #9aa3af; }
</style></head><body>
<header>
  <div><h1>${esc(agent)}</h1>
  <div style="color:${LIGHT_BLUE}">${esc(teamLabel)} — QA Performance</div></div>
  <div style="text-align:right"><div class="brand">Landing · QA</div>
  <div style="font-size:15px;font-weight:600">${monthLabel}</div></div>
</header>

<div class="cards">
  <div class="card"><div class="v">${m.toFixed(1)}</div><div class="l">Avg overall score</div></div>
  <div class="card"><div class="v">${n}</div><div class="l">Calls evaluated</div></div>
  <div class="card"><div class="v">${std.toFixed(1)}</div><div class="l">Std deviation</div></div>
  <div class="card"><div class="v">${Math.min(...overall).toFixed(0)}–${Math.max(...overall).toFixed(0)}</div><div class="l">Score range</div></div>
</div>

<h2>Score progression — ${monthLabel}</h2>
${sparkline(overall)}

<h2>Per-section performance</h2>
<table><tbody>
${tableRows}
</tbody></table>

${coachingHtml}

${assessmentHtml(assessment)}

<footer>Generated ${genStamp} · Source: qa_evaluations
(${n} finalized evaluations, ${monthLabel}) · Trend = second-half vs first-half average
· Scores computed by the Landing QA engine</footer>
</body></html>`;
}

// Agent one-pager — port of backend/services/onepager.py (JulyR2R3 §3).
// Renders the print-ready monthly HTML from the SAME analytics frame the
// dashboards use, plus the PERSISTED month assessment (never generates —
// a page view must never spend a Gemini call).

import type { FrameRow } from "./historyFrame.js";
import type { TeamConfig } from "./teamConfig.js";
import { monthInBucketTz, BUCKET_TZ } from "./teamStats.js";

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

${assessmentHtml(assessment)}

<footer>Generated ${genStamp} · Source: qa_evaluations
(${n} finalized evaluations, ${monthLabel}) · Trend = second-half vs first-half average
· Scores computed by the Landing QA engine</footer>
</body></html>`;
}

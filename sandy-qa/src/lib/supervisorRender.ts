// Supervisor Deliverables — what the supervisor types (FIELD_SPEC: the
// only inputs a system cannot know) and how a finished deliverable is
// pushed: Slack Block Kit (one post, the channel the team already reads)
// and one row per instance on a tab of the EOD spreadsheet (the same
// workbook the daily call metrics land in). The page editors render
// FIELD_SPEC, so labels live in one place. SupervisorDeliverables.md §5/§6.

import type { Kind } from "./supervisorFacts.js";

export interface Field {
  key: string;
  label: string;
  type: "textarea" | "text" | "number" | "checkbox" | "rows";
  help?: string;
  columns?: { key: string; label: string; width?: number }[];
}
export interface Section {
  key: string;
  title: string;
  auto?: string; // which auto-facts panel to show above the fields
  fields: Field[];
}

const T = (key: string, label: string, help?: string): Field => ({ key, label, type: "textarea", help });
const S = (key: string, label: string, help?: string): Field => ({ key, label, type: "text", help });

export const FIELD_SPEC: Record<Kind, Section[]> = {
  daily: [
    {
      key: "calls", title: "1. Call metrics", auto: "calls",
      fields: [
        T("main_issues", "Main issues impacting the shift"),
        T("trends", "Call-volume or operational trends"),
        T("target_impact", "Situations that impacted the team's ability to meet the target"),
      ],
    },
    {
      key: "attendance", title: "2. Attendance & schedule adherence", auto: "attendance",
      fields: [
        T("absences", "Absences", "who, and whether it was notified in the attendance channel"),
        T("delays", "Delays / late arrivals"),
        T("coverage_issues", "Coverage issues"),
        T("staffing_impact", "Situations that impacted staffing or coverage"),
      ],
    },
    {
      key: "tickets", title: "3. Ticket status", auto: "tickets",
      fields: [
        { key: "tickets_manual", label: "Counts (only if the live section is unavailable)", type: "rows",
          columns: [{ key: "category", label: "Type / category" }, { key: "new", label: "New", width: 60 }, { key: "working", label: "Working", width: 60 }, { key: "pending", label: "Pending", width: 60 }, { key: "resolved", label: "Resolved", width: 60 }] },
        T("ticket_notes", "Significant backlog or ticket issue requiring attention"),
      ],
    },
    {
      key: "slack", title: "4. Pending Slack tags", auto: "slack",
      fields: [
        T("tags_ms", "Pending @member-support tags"),
        T("tags_mgrs", "Pending @member-support-mgrs tags"),
        T("aged_tags", "Aged, unresolved or escalated tags"),
        T("recurring", "Recurring issue to follow up on"),
      ],
    },
    {
      key: "handoff", title: "Handoff", auto: "follow_ups",
      fields: [
        T("resolved", "What was resolved"),
        T("follow_up", "What still needs follow-up (next supervisor / management)"),
      ],
    },
  ],
  weekly: [
    {
      key: "abandon", title: "2.1 Abandon rate", auto: "abandon",
      fields: [
        T("impacted_when", "Days / hours where performance was impacted"),
        T("reasons", "Main reasons contributing to the missed target"),
        T("factors", "Operational or staffing factors"),
        T("actions_taken", "Actions already taken"),
        T("next_actions", "Recommended actions for next week"),
        S("next_target", "Target for next week"),
      ],
    },
    {
      key: "productivity", title: "2.2 Productivity", auto: "productivity",
      fields: [
        S("above", "Agents exceeding expectations"),
        S("below", "Agents below expectations"),
        T("gap_when", "When the productivity gap occurred"),
        T("factors", "Operational factors impacting productivity"),
        T("patterns", "Recurring patterns or trends"),
        T("coaching_plans", "Coaching or action plans required"),
      ],
    },
    {
      key: "qa", title: "2.3 Quality assurance", auto: "qa",
      fields: [
        { key: "qa_reviews", label: "Per review", type: "rows", help: "one row per QA review that led to coaching",
          columns: [{ key: "agent", label: "Agent" }, { key: "opportunity", label: "Main opportunity" }, { key: "behavior", label: "Behavior / process to improve" }, { key: "coaching", label: "Coaching provided" }, { key: "action_plan", label: "Action plan" }, { key: "follow_up", label: "Follow-up" }, { key: "training", label: "Training / refresher topic" }] },
        T("team_topics", "Recurring issues to address at team level / topics needing training or reinforcement"),
      ],
    },
    {
      key: "csat", title: "2.4 CSAT review", auto: "csat",
      fields: [
        { key: "csat_reviews", label: "Agents below target", type: "rows",
          columns: [{ key: "agent", label: "Agent" }, { key: "score", label: "Score", width: 60 }, { key: "calls", label: "Calls / interactions reviewed" }, { key: "reason", label: "Reason behind the low score" }, { key: "coaching", label: "Coaching provided" }, { key: "action_plan", label: "Action plan / follow-up" }] },
        T("csat_notes", "Notes"),
      ],
    },
    {
      key: "admin", title: "7. Administrative", auto: "action_plans",
      fields: [
        { key: "kronos_done", label: "Kronos time sheets completed, submitted and approved (Monday noon after payday)", type: "checkbox" },
        T("overtime", "Overtime worked by assigned agents (documented and communicated)"),
        T("improvements", "Additional topics / operational improvements for the Supervisor meeting"),
      ],
    },
  ],
  monthly: [
    {
      key: "abandon", title: "5.1 Monthly abandon rate", auto: "abandon",
      fields: [
        T("trend", "Overall trend"),
        T("largest_impact", "Weeks / days with the largest impact"),
        T("factors", "Main contributing factors"),
        T("actions", "Actions implemented"),
        T("results", "Results of previous actions"),
        T("opportunities", "Opportunities for the remainder of the month"),
      ],
    },
  ],
  biweekly: [
    {
      key: "meeting", title: "4.1 Team meeting preparation", auto: "rotation",
      fields: [
        S("presenter", "Supervisor presenting"),
        T("topics", "Required meeting topics / key learnings / expectations"),
        S("material", "Presentation / material link", "reviewed at the Tuesday Supervisor meeting before the team meeting"),
        T("training_topics", "Training / refresher topics for the team"),
      ],
    },
    {
      key: "schedule", title: "3.1 / 5. Schedule review", auto: "fortnight",
      fields: [
        { key: "schedule_rows", label: "Schedule changes", type: "rows",
          columns: [{ key: "area", label: "Area" }, { key: "current", label: "Current schedule" }, { key: "proposed", label: "Proposed change" }, { key: "data", label: "Data supporting the change" }, { key: "impact", label: "Expected impact" }] },
      ],
    },
  ],
};

// ── formatting ─────────────────────────────────────────────────────────────

const pctS = (v: any) => (v === null || v === undefined ? "—" : `${v}%`);
const numS = (v: any, d = 1) => (v === null || v === undefined || Number.isNaN(Number(v)) ? "—" : String(Math.round(Number(v) * 10 ** d) / 10 ** d));
const secS = (v: any) => {
  if (v === null || v === undefined) return "—";
  const s = Math.round(Number(v));
  return s >= 60 ? `${Math.floor(s / 60)} m ${String(s % 60).padStart(2, "0")} s` : `${s} s`;
};
const clean = (s: any) => String(s ?? "").trim();
const or = (s: any, dash = "—") => (clean(s) ? clean(s) : dash);
const cut = (s: string, n: number) => (s.length > n ? s.slice(0, n - 1) + "…" : s);

export interface ReportRow {
  id: number;
  kind: Kind;
  period_key: string;
  shift: string | null;
  supervisor: string | null;
  owner_email: string;
  status: string;
  submitted_at?: string | null;
  auto: any;
  manual: any;
}

export interface Rendered {
  title: string;
  text: string; // plain fallback (also what the sheet's summary column carries)
  blocks: any[];
}

const section = (md: string) => ({ type: "section", text: { type: "mrkdwn", text: cut(md, 2900) } });
const header = (t: string) => ({ type: "header", text: { type: "plain_text", text: cut(t, 150), emoji: true } });
const context = (md: string) => ({ type: "context", elements: [{ type: "mrkdwn", text: cut(md, 2000) }] });
const divider = () => ({ type: "divider" });

function rowsMd(rows: any[], cols: { key: string; label: string }[], max = 8): string {
  if (!Array.isArray(rows) || !rows.length) return "";
  return rows
    .slice(0, max)
    .map((r) => "• " + cols.map((c) => `*${c.label}:* ${or(r[c.key], "—")}`).join(" · "))
    .join("\n");
}

function renderDaily(r: ReportRow, teamName: string): Rendered {
  const a = r.auto ?? {};
  const m = r.manual ?? {};
  const shift = a.period?.shift;
  const parts = a.calls?.shift ?? a.calls?.full_day;
  const scope = shift ? `${shift.label} · ${shift.window}` : "Full day";
  const title = `${teamName} End-of-Shift — ${scope} · ${r.period_key}`;
  const target = a.calls?.target;
  const rate = parts ? pctS(parts.abandon_pct) : "—";
  const callsMd =
    `*Call metrics (Member Support line)*\n` +
    (parts
      ? `• Abandon rate: *${rate}*${target !== null && target !== undefined ? ` (target ${target}%)` : ""} — ${parts.abandoned} of ${parts.inbound} inbound\n` +
        `• Total abandoned calls: ${parts.abandoned} (short < 6 s: ${parts.short_abandoned ?? 0}) · avg wait before abandon ${secS(parts.avg_wait_abandoned_s)} · longest ${secS(parts.longest_wait_abandoned_s)}\n` +
        `• Service level: ${pctS(parts.sl_pct)} (target ${a.calls?.sl_target ?? "—"}%) · ASA ${secS(parts.asa_s)} · agents on duty ${parts.agents_on_duty ?? "—"}\n` +
        (a.calls?.mtd?.abandon_pct !== undefined ? `• Month to date: ${pctS(a.calls.mtd.abandon_pct)} (${a.calls.mtd.abandoned} of ${a.calls.mtd.inbound})\n` : "")
      : `• _${a.calls?.note ?? "no call data yet"}_\n`) +
    `• Main issues: ${or(m.main_issues)}\n• Trends: ${or(m.trends)}\n• Target impact: ${or(m.target_impact)}`;
  const attMd =
    `*Attendance & schedule adherence*\n` +
    `• Absences: ${or(m.absences, "none reported")}\n• Delays / late arrivals: ${or(m.delays, "none reported")}\n` +
    `• Coverage issues: ${or(m.coverage_issues, "none")}\n• Staffing impact: ${or(m.staffing_impact, "none")}`;
  let ticketsMd = `*Ticket status*\n`;
  const tk = a.tickets;
  if (tk?.status === "ready") {
    ticketsMd += `_MC queue ${tk.queue_id}, as of ${tk.as_of ? String(tk.as_of).slice(0, 16).replace("T", " ") : "?"} · New / Working / Need action_\n`;
    ticketsMd += (tk.categories ?? []).map((c: any) => `• ${c.label}: ${c.new} / ${c.working} / ${c.need_action}`).join("\n");
    ticketsMd += `\n• Total open in queue: ${tk.total_open}`;
  } else if (Array.isArray(m.tickets_manual) && m.tickets_manual.length) {
    ticketsMd += rowsMd(m.tickets_manual, [{ key: "category", label: "Type" }, { key: "new", label: "New" }, { key: "working", label: "Working" }, { key: "pending", label: "Pending" }, { key: "resolved", label: "Resolved" }]);
  } else ticketsMd += `• _unavailable: ${tk?.error ?? "not fetched"}_`;
  ticketsMd += `\n• Backlog / attention: ${or(m.ticket_notes, "none")}`;
  let slackMd = `*Pending Slack tags*\n`;
  const sl = a.slack;
  if (sl?.status === "ready") {
    slackMd += `• Unattended @member-support / @member-support-mgrs mentions: *${sl.count}*` + (sl.count ? "\n" + (sl.items ?? []).slice(0, 10).map((i: any) => `  ◦ #${i.channel} ${String(i.ts_iso).slice(11, 16)} ${i.author} → <${i.permalink}|open>`).join("\n") : "");
  } else slackMd += `• _audit unavailable: ${sl?.error ?? "not fetched"}_`;
  slackMd += `\n• @member-support: ${or(m.tags_ms, "none")}\n• @member-support-mgrs: ${or(m.tags_mgrs, "none")}\n• Aged / escalated: ${or(m.aged_tags, "none")}\n• Recurring: ${or(m.recurring, "none")}`;
  const handoffMd = `*Handoff*\n• Resolved: ${or(m.resolved)}\n• Still needs follow-up: ${or(m.follow_up)}`;
  const blocks = [header(title), section(callsMd), section(attMd), section(ticketsMd), section(slackMd), section(handoffMd), context(`Submitted by ${r.owner_email}${r.submitted_at ? " · " + r.submitted_at.slice(0, 16).replace("T", " ") + " UTC" : ""} · qa-scoring supervisor deliverables`)];
  const text = [title, callsMd, attMd, ticketsMd, slackMd, handoffMd].join("\n\n").replace(/\*/g, "");
  return { title, text, blocks };
}

function renderWeekly(r: ReportRow, teamName: string): Rendered {
  const a = r.auto ?? {};
  const m = r.manual ?? {};
  const w = a.abandon?.week ?? {};
  const who = r.supervisor ? ` · ${r.supervisor}` : "";
  const title = `${teamName} Weekly Review — ${r.period_key}${who}`;
  const verdict = w.met === null || w.met === undefined ? "target not set" : w.met ? "✅ target met" : "❌ target missed";
  const abandonMd =
    `*2.1 Abandon rate* — ${verdict}\n` +
    `• Week: *${pctS(w.abandon_pct)}* (target ${pctS(a.abandon?.target)}) — ${w.abandoned ?? 0} of ${w.inbound ?? 0} inbound · SL ${pctS(w.sl_pct)}` +
    (w.prev_week ? ` · prev week ${pctS(w.prev_week.abandon_pct)}` : "") + "\n" +
    `• Month to date: ${pctS(a.abandon?.mtd?.abandon_pct)} (${a.abandon?.mtd?.abandoned ?? 0} of ${a.abandon?.mtd?.inbound ?? 0})\n` +
    `• Worst days: ${(w.worst_days ?? []).map((d: any) => `${d.date} ${pctS(d.abandon_pct)}`).join(", ") || "—"}` +
    ((w.days_missing ?? []).length ? ` · _no data for ${w.days_missing.length} day(s)_` : "") + "\n" +
    `• Impacted when: ${or(m.impacted_when)}\n• Reasons: ${or(m.reasons)}\n• Factors: ${or(m.factors)}\n• Actions taken: ${or(m.actions_taken)}\n• Next week: ${or(m.next_actions)} (target ${or(m.next_target, pctS(a.abandon?.target))})`;
  const p = a.productivity ?? {};
  const prodMd =
    `*2.2 Productivity*\n` +
    (p.status === "ready"
      ? `• Team: *${numS(p.team?.per_hour, 2)} calls/h*${p.target !== null && p.target !== undefined ? ` (target ${p.target})` : ""} · ${p.team?.answered ?? 0} answered + ${p.team?.outbound ?? 0} outbound over ${numS((p.team?.on_duty_min ?? 0) / 60, 0)} on-duty hours\n` +
        `• Above target: ${(p.above_target ?? []).join(", ") || "—"} · Below target: ${(p.below_target ?? []).join(", ") || "—"}\n`
      : `• _Dialpad pull ${p.status}${p.error ? `: ${p.error}` : ""}_\n`) +
    `• Exceeding: ${or(m.above)} · Below: ${or(m.below)}\n• Gap occurred: ${or(m.gap_when)}\n• Factors: ${or(m.factors)}\n• Patterns: ${or(m.patterns)}\n• Coaching / action plans: ${or(m.coaching_plans)}` +
    (p.wfm_dashboard_url ? `\n• <${p.wfm_dashboard_url}|Workforce Management dashboard>` : "");
  const q = a.qa ?? {};
  const below = (q.per_agent ?? []).filter((x: any) => x.meets_target === false).map((x: any) => `${x.name} (${x.human_reviews})`);
  const qaMd =
    `*2.3 Quality assurance*\n` +
    `• ${q.totals?.human_reviews ?? 0} supervisor reviews across ${q.totals?.agents ?? 0} agents (target ${q.target_reviews ?? "—"}/agent) · ${q.totals?.ai_reviews ?? 0} AI scorings · avg score ${numS(q.totals?.avg_score)}\n` +
    `• Under the review target: ${below.join(", ") || "none"}\n` +
    `• Coaching conducted: ${a.coaching?.counts?.conducted ?? 0} · pending sessions ${a.coaching?.counts?.pending ?? 0} · confirmations due ${a.coaching?.counts?.due_confirmations ?? 0}\n` +
    (rowsMd(m.qa_reviews, [{ key: "agent", label: "Agent" }, { key: "opportunity", label: "Opportunity" }, { key: "coaching", label: "Coaching" }, { key: "action_plan", label: "Action plan" }, { key: "follow_up", label: "Follow-up" }]) || "• _no per-review rows entered_") +
    `\n• Team-level topics: ${or(m.team_topics)}`;
  const c = a.csat ?? {};
  const csatMd =
    `*2.4 CSAT*\n` +
    (c.status === "ready"
      ? `• Team avg *${numS(c.team_avg, 2)}* (target ${c.target ?? "—"}) from ${c.responses ?? 0} survey responses (${c.attributed ?? 0} attributed to agents)\n` +
        `• Below target: ${(c.below_target ?? []).map((x: any) => `${x.name} ${x.avg} (${x.responses})`).join(", ") || "none"}\n`
      : `• _Dialpad pull ${c.status}${c.error ? `: ${c.error}` : ""}_\n`) +
    (rowsMd(m.csat_reviews, [{ key: "agent", label: "Agent" }, { key: "score", label: "Score" }, { key: "reason", label: "Reason" }, { key: "coaching", label: "Coaching" }, { key: "action_plan", label: "Action / follow-up" }]) || "• _no per-agent rows entered_") +
    `\n• Notes: ${or(m.csat_notes)}`;
  const plans = a.action_plans?.open ?? [];
  const plansMd =
    `*6. Action plans (open)*\n` +
    (plans.length ? plans.slice(0, 10).map((p: any) => `• [${p.area}] ${p.agent_name ? p.agent_name + ": " : ""}${p.issue} → ${p.action} (${p.owner}${p.follow_up_date ? ", " + p.follow_up_date : ""}) _${p.status}_`).join("\n") : "• none open") +
    `\n*7. Administrative*\n• Kronos timesheets: ${m.kronos_done ? "done ✅" : "not confirmed"} · Overtime: ${or(m.overtime, "none")}\n• Improvements / topics: ${or(m.improvements)}`;
  const blocks = [header(title), section(abandonMd), section(prodMd), section(qaMd), section(csatMd), section(plansMd), context(`Submitted by ${r.owner_email}${r.submitted_at ? " · " + r.submitted_at.slice(0, 16).replace("T", " ") + " UTC" : ""} · ${a.period?.start} → ${a.period?.end}`)];
  const text = [title, abandonMd, prodMd, qaMd, csatMd, plansMd].join("\n\n").replace(/\*/g, "");
  return { title, text, blocks };
}

function renderMonthly(r: ReportRow, teamName: string): Rendered {
  const a = r.auto ?? {};
  const m = r.manual ?? {};
  const mo = a.abandon?.month ?? {};
  const who = r.supervisor ? ` · ${r.supervisor}` : "";
  const title = `${teamName} Monthly Abandon Rate — ${r.period_key}${who}`;
  const verdict = mo.met === null || mo.met === undefined ? "target not set" : mo.met ? "✅ on target" : "❌ over target";
  const md =
    `*5.1 Monthly abandon rate* — ${verdict}\n` +
    `• ${a.period?.is_closed ? "Month" : "Month to date"}: *${pctS(mo.abandon_pct)}* (target ${pctS(a.abandon?.target)}) — ${mo.abandoned ?? 0} of ${mo.inbound ?? 0} inbound · SL ${pctS(mo.sl_pct)} · prev month ${pctS(a.abandon?.prev_month?.abandon_pct)}\n` +
    `• By week: ${(a.abandon?.weeks ?? []).map((w: any) => `${w.week} ${pctS(w.abandon_pct)}`).join(" · ") || "—"}\n` +
    `• Worst days: ${(mo.worst_days ?? []).map((d: any) => `${d.date} ${pctS(d.abandon_pct)}`).join(", ") || "—"}\n` +
    `• Trend: ${or(m.trend)}\n• Largest impact: ${or(m.largest_impact)}\n• Contributing factors: ${or(m.factors)}\n• Actions implemented: ${or(m.actions)}\n• Results of previous actions: ${or(m.results)}\n• Opportunities for the rest of the month: ${or(m.opportunities)}`;
  const plans = a.action_plans ?? {};
  const plansMd =
    `*Action plans*\n• Open: ${(plans.open ?? []).length} · closed this month: ${(plans.closed_this_month ?? []).length}\n` +
    (plans.closed_this_month ?? []).slice(0, 6).map((p: any) => `• ✔ ${p.issue} → ${or(p.result, "no result recorded")}`).join("\n");
  const blocks = [header(title), section(md), section(plansMd), context(`Submitted by ${r.owner_email}${r.submitted_at ? " · " + r.submitted_at.slice(0, 16).replace("T", " ") + " UTC" : ""}`)];
  return { title, text: [title, md, plansMd].join("\n\n").replace(/\*/g, ""), blocks };
}

function renderTracker(r: ReportRow, teamName: string, tracker: any): Rendered {
  const title = `${teamName} Supervisor Meeting Tracker — ${r.period_key}`;
  const t = tracker ?? {};
  const perf = `*1. Performance*\n` + (t.performance ?? []).map((x: any) => `• *${x.area}:* ${x.current} · target ${x.target} · ${x.gap}${clean(x.notes) ? " — " + clean(x.notes) : ""}`).join("\n");
  const coach = `*2. Performance & coaching actions*\n` + (t.coaching_actions ?? []).map((x: any) => `• *${x.area}:* ${or(x.agent, "—")} · ${or(x.issue)} · ${or(x.action)} · follow-up ${or(x.follow_up)}`).join("\n");
  const ops = `*3. Operational review*\n` + (t.operational ?? []).map((x: any) => `• *${x.area}:* ${or(x.finding)} · impact ${or(x.impact)} · ${or(x.action)} · follow-up ${or(x.follow_up)}`).join("\n");
  const dec = `*4. Bi-weekly actions & decisions*\n` + ((t.decisions ?? []).map((x: any, i: number) => `• ${i + 1}. ${or(x.topic)} → ${or(x.action)} (${or(x.owner)}${clean(x.due) ? ", " + clean(x.due) : ""})`).join("\n") || "• none");
  const sch = `*5. Schedule review*\n` + (t.schedule ?? []).map((x: any) => `• *${x.area}:* ${or(x.current)} → ${or(x.proposed)} · data: ${or(x.data)} · expected: ${or(x.impact)}`).join("\n");
  const blocks = [header(title), context(`Presenter: ${or(t.presenter, "tbd")} · prepared by ${r.owner_email}`), section(perf), section(coach), section(ops), section(dec), section(sch)];
  return { title, text: [title, perf, coach, ops, dec, sch].join("\n\n").replace(/\*/g, ""), blocks };
}

export function renderReport(r: ReportRow, teamName: string, extra: { tracker?: any } = {}): Rendered {
  if (r.kind === "daily") return renderDaily(r, teamName);
  if (r.kind === "weekly") return renderWeekly(r, teamName);
  if (r.kind === "monthly") return renderMonthly(r, teamName);
  return renderTracker(r, teamName, extra.tracker);
}

// ── sheet rows (one row per instance; col A is the instance key) ───────────

export const SHEET_HEADERS: Record<Kind, string[]> = {
  daily: ["key", "date", "shift", "owner", "status", "submitted_at", "inbound", "abandoned", "abandon_pct", "sl_pct", "agents_on_duty", "tickets_open", "unattended_mentions", "main_issues", "absences", "delays", "coverage_issues", "ticket_notes", "aged_tags", "resolved", "follow_up", "summary"],
  weekly: ["key", "week", "supervisor", "owner", "status", "submitted_at", "abandon_pct", "target", "met", "abandoned", "inbound", "sl_pct", "mtd_abandon_pct", "prod_team_per_hour", "prod_below", "qa_reviews", "qa_agents_below", "qa_avg", "csat_avg", "csat_below", "reasons", "actions_taken", "next_actions", "team_topics", "kronos_done", "summary"],
  monthly: ["key", "month", "supervisor", "owner", "status", "submitted_at", "abandon_pct", "target", "met", "abandoned", "inbound", "sl_pct", "trend", "factors", "actions", "results", "opportunities", "summary"],
  biweekly: ["key", "meeting_date", "presenter", "owner", "status", "submitted_at", "abandon_pct_14d", "decisions", "summary"],
};

export function instanceKey(r: ReportRow): string {
  return [r.period_key, r.shift ?? "", r.supervisor ?? ""].filter((x, i) => i === 0 || x).join(" · ");
}

export function sheetRow(r: ReportRow, rendered: Rendered, tracker?: any): (string | number | null)[] {
  const a = r.auto ?? {};
  const m = r.manual ?? {};
  const key = instanceKey(r);
  const sub = r.submitted_at ?? "";
  const cap = (s: string) => s.slice(0, 45_000);
  if (r.kind === "daily") {
    const p = a.calls?.shift ?? a.calls?.full_day ?? {};
    return [key, r.period_key, r.shift ?? "", r.owner_email, r.status, sub, p.inbound ?? "", p.abandoned ?? "", p.abandon_pct ?? "", p.sl_pct ?? "", p.agents_on_duty ?? "", a.tickets?.total_open ?? "", a.slack?.count ?? "", clean(m.main_issues), clean(m.absences), clean(m.delays), clean(m.coverage_issues), clean(m.ticket_notes), clean(m.aged_tags), clean(m.resolved), clean(m.follow_up), cap(rendered.text)];
  }
  if (r.kind === "weekly") {
    const w = a.abandon?.week ?? {};
    const p = a.productivity ?? {};
    const q = a.qa ?? {};
    const c = a.csat ?? {};
    return [key, r.period_key, r.supervisor ?? "", r.owner_email, r.status, sub, w.abandon_pct ?? "", a.abandon?.target ?? "", w.met === null || w.met === undefined ? "" : w.met ? "Y" : "N", w.abandoned ?? "", w.inbound ?? "", w.sl_pct ?? "", a.abandon?.mtd?.abandon_pct ?? "", p.team?.per_hour ?? "", (p.below_target ?? []).join(", "), q.totals?.human_reviews ?? "", q.totals?.agents_below_target ?? "", q.totals?.avg_score ?? "", c.team_avg ?? "", (c.below_target ?? []).map((x: any) => x.name).join(", "), clean(m.reasons), clean(m.actions_taken), clean(m.next_actions), clean(m.team_topics), m.kronos_done ? "Y" : "N", cap(rendered.text)];
  }
  if (r.kind === "monthly") {
    const mo = a.abandon?.month ?? {};
    return [key, r.period_key, r.supervisor ?? "", r.owner_email, r.status, sub, mo.abandon_pct ?? "", a.abandon?.target ?? "", mo.met === null || mo.met === undefined ? "" : mo.met ? "Y" : "N", mo.abandoned ?? "", mo.inbound ?? "", mo.sl_pct ?? "", clean(m.trend), clean(m.factors), clean(m.actions), clean(m.results), clean(m.opportunities), cap(rendered.text)];
  }
  return [key, r.period_key, clean(tracker?.presenter ?? m.presenter), r.owner_email, r.status, sub, a.fortnight?.abandon_pct ?? "", (tracker?.decisions ?? []).map((d: any) => `${d.topic} → ${d.action} (${d.owner})`).join(" | "), cap(rendered.text)];
}

// Supervisor Deliverables — pages + API (SupervisorDeliverables.md §5).
// Gate: `coach` (admin | qa | team-scoped manager) — the same capability
// the coaching surfaces use; supervisors are the manager role. Every
// mutation writes a qa_sup_events row. Facts are computed live for drafts
// and frozen into `auto` at submit; the live daily sections (tickets,
// Slack) are fetched on demand and persisted so the snapshot carries them.

import { accessEmail, canCoach, resolveAccess } from "../lib/rbac.js";
import {
  buildBiweeklyFacts,
  buildDailyFacts,
  buildMonthlyFacts,
  buildTracker,
  buildWeeklyFacts,
  ensurePull,
  fetchDailyLive,
  isoWeekKey,
  labelForEmail,
  listActionPlans,
  loadTeamContext,
  nextWeekday,
  periodRange,
  PLAN_AREAS,
  PLAN_STATUSES,
  resetPull,
  saveTargets,
  supervisorLabels,
  todayLocal,
  validKey,
  weekRange,
  type Kind,
  type TeamContext,
} from "../lib/supervisorFacts.js";
import { FIELD_SPEC, instanceKey, renderReport, sheetRow, SHEET_HEADERS, type ReportRow } from "../lib/supervisorRender.js";
// @ts-ignore vite ?raw
import hubHtml from "../../pages/supervisor.html?raw";
// @ts-ignore vite ?raw
import reportHtml from "../../pages/supervisor_report.html?raw";
// @ts-ignore vite ?raw
import trackerHtml from "../../pages/supervisor_tracker.html?raw";

export interface SupEnv {
  DIALPAD_API_KEY?: string;
  GSHEETS_SA_JSON?: string;
  SLACK_BOT_TOKEN?: string;
  SLACK_USER_TOKEN?: string;
  SNOWFLAKE_MCP_TOKEN?: string;
}

const KINDS: Kind[] = ["daily", "weekly", "biweekly", "monthly"];

const json = (data: unknown, status = 200) =>
  new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json" } });
const html = (body: string, status = 200) =>
  new Response(body, { status, headers: { "Content-Type": "text/html; charset=utf-8" } });
const nowIso = () => new Date().toISOString();
const parse = (s: string | null | undefined, fallback: any = null) => {
  try {
    return s ? JSON.parse(s) : fallback;
  } catch {
    return fallback;
  }
};

function deniedPage(teamId: string): Response {
  return html(
    `<!DOCTYPE html><html><head><title>Supervisor deliverables — restricted</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&display=swap" rel="stylesheet"></head>
<body style="font-family:'DM Mono',monospace;background:#E7EFFB;color:#15192D;display:grid;place-items:center;min-height:100vh;margin:0">
<div style="background:#fff;border:1px solid #c9d5e8;border-radius:12px;padding:32px;max-width:560px">
<h2 style="margin-top:0">Supervisor deliverables are restricted</h2>
<p style="font-size:.85rem;line-height:1.55">End-of-shift reports, weekly reviews and the meeting tracker carry team performance
and coaching detail, so this page is limited to QA staff and team supervisors (the manager role).</p>
<p style="font-size:.8rem;color:#5a6478">Ask a QA admin to grant you the <b>manager</b> role for <b>${teamId}</b> on /admin.</p>
<p style="color:#5a6478;font-size:.72rem;margin-top:18px"><a href="/" style="color:#1A61D9">&larr; Back to dashboards</a></p>
</div></body></html>`,
    403
  );
}

async function gate(request: Request, db: D1Database, teamId: string, lookupAllow?: string) {
  const access = await resolveAccess(request, db, lookupAllow);
  if (!canCoach(access, teamId)) return { deny: true as const, access };
  const email = access.email || accessEmail(request);
  return { deny: false as const, access, email };
}

async function logEvent(db: D1Database, e: { report_id?: number | null; plan_id?: number | null; action: string; detail?: any; actor: string }) {
  await db
    .prepare("INSERT INTO qa_sup_events (report_id, plan_id, action, detail, actor_email) VALUES (?,?,?,?,?)")
    .bind(e.report_id ?? null, e.plan_id ?? null, e.action, e.detail ? JSON.stringify(e.detail) : null, e.actor || "?")
    .run();
}

async function readBody(request: Request): Promise<any> {
  try {
    return (await request.json()) ?? {};
  } catch {
    return {};
  }
}

const rowOf = (r: any): ReportRow => ({
  id: r.id,
  kind: r.kind,
  period_key: r.period_key,
  shift: r.shift ?? null,
  supervisor: r.supervisor ?? null,
  owner_email: r.owner_email,
  status: r.status,
  submitted_at: r.submitted_at ?? null,
  auto: parse(r.auto, null),
  manual: parse(r.manual, {}),
});

async function computeFacts(db: D1Database, ctx: TeamContext, row: ReportRow, today: string): Promise<any> {
  if (row.kind === "daily") return buildDailyFacts(db, ctx, row.period_key, row.shift, today);
  if (row.kind === "weekly") return buildWeeklyFacts(db, ctx, row.period_key, row.supervisor, today);
  if (row.kind === "monthly") return buildMonthlyFacts(db, ctx, row.period_key, row.supervisor, today);
  return buildBiweeklyFacts(db, ctx, row.period_key, today);
}

/** Drafts: live facts + the persisted live sections. Submitted: the frozen snapshot. */
async function factsFor(db: D1Database, ctx: TeamContext, row: ReportRow, today: string, force = false): Promise<any> {
  if (row.status === "submitted" && row.auto && !row.auto.live_only && !force) return row.auto;
  const facts = await computeFacts(db, ctx, row, today);
  const live = row.auto?.live ?? (row.auto?.live_only ? row.auto : null);
  if (live?.tickets) facts.tickets = live.tickets;
  if (live?.slack) facts.slack = live.slack;
  return facts;
}

async function kickTicker(db: D1Database, request: Request, reason: string) {
  try {
    const { triggerTicker } = await import("../lib/maintenance.js");
    return await triggerTicker(db, request, reason);
  } catch (err) {
    return { status: "error", note: String((err as any)?.message ?? err).slice(0, 120) };
  }
}

async function getReport(db: D1Database, teamId: string, id: number): Promise<any | null> {
  return await db.prepare("SELECT * FROM qa_sup_reports WHERE id = ? AND team_id = ?").bind(id, teamId).first<any>();
}

// ── entry ──────────────────────────────────────────────────────────────────

export async function handleSupervisorRoutes(
  request: Request,
  db: D1Database,
  url: URL,
  lookupAllow: string | undefined,
  env: SupEnv
): Promise<Response | null> {
  const path = url.pathname;
  let m: RegExpMatchArray | null;

  // ── pages ────────────────────────────────────────────────────────────────
  m = path.match(/^\/supervisor\/([^/]+)(\/report\/\d+|\/tracker\/\d{4}-\d{2}-\d{2})?\/?$/);
  if (m && request.method === "GET") {
    const teamId = m[1];
    try {
      await loadTeamContext(db, teamId);
    } catch {
      return null;
    }
    const g = await gate(request, db, teamId, lookupAllow);
    if (g.deny) return deniedPage(teamId);
    if (!m[2]) return html(hubHtml);
    return html(m[2].startsWith("/report/") ? reportHtml : trackerHtml);
  }

  m = path.match(/^\/api\/([^/]+)\/sup(\/.*)?$/);
  if (!m) return null;
  const teamId = m[1];
  const sub = m[2] ?? "";
  let ctx: TeamContext;
  try {
    ctx = await loadTeamContext(db, teamId);
  } catch {
    return json({ detail: `unknown team ${teamId}` }, 404);
  }
  const g = await gate(request, db, teamId, lookupAllow);
  if (g.deny) return json({ detail: "Supervisor deliverables are restricted to QA staff and team supervisors (manager role)." }, 403);
  const actor = g.email ?? "";
  const today = todayLocal(ctx.tz);

  // GET /status — config, targets, who am I, which sources are wired
  if (sub === "/status" && request.method === "GET") {
    const sups = await supervisorLabels(db, teamId);
    const myLabel = await labelForEmail(db, teamId, actor);
    const eod = await db
      .prepare("SELECT report_date, status FROM qa_eod_reports WHERE team_id = ? ORDER BY report_date DESC LIMIT 1")
      .bind(teamId)
      .first<any>();
    const openPlans = await db
      .prepare("SELECT COUNT(*) AS n FROM qa_sup_action_plans WHERE team_id = ? AND status IN ('open','in_progress')")
      .bind(teamId)
      .first<any>();
    return json({
      team_id: teamId,
      team: ctx.name,
      timezone: ctx.tz,
      today,
      week: isoWeekKey(today),
      month: today.slice(0, 7),
      next_meeting: nextWeekday(today, ctx.cfg.meeting_weekday),
      shifts: ctx.shifts,
      targets: ctx.cfg.targets,
      config: {
        slack_channel: ctx.cfg.slack_channel,
        spreadsheet_id: ctx.spreadsheetId,
        sheet_tab_prefix: ctx.cfg.sheet_tab_prefix,
        wfm_dashboard_url: ctx.cfg.wfm_dashboard_url,
        attendance_channel: ctx.cfg.attendance_channel,
        meeting_weekday: ctx.cfg.meeting_weekday,
      },
      supervisors: sups,
      me: { email: actor, label: myLabel, role: g.access.role, is_admin: g.access.role === "admin" },
      sources: {
        dialpad: !!env.DIALPAD_API_KEY,
        sheets: !!env.GSHEETS_SA_JSON && !!ctx.spreadsheetId,
        slack_post: !!env.SLACK_BOT_TOKEN && !!ctx.cfg.slack_channel,
        slack_audit: !!env.SLACK_USER_TOKEN,
        snowflake: !!env.SNOWFLAKE_MCP_TOKEN,
      },
      eod: eod ? { latest_date: eod.report_date, status: eod.status } : null,
      open_action_plans: Number(openPlans?.n ?? 0),
    });
  }

  // PUT /targets
  if (sub === "/targets" && request.method === "PUT") {
    if (!(g.access.role === "admin" || g.access.role === "manager" || g.access.role === "qa"))
      return json({ detail: "Targets are set by admins, QA staff or team managers." }, 403);
    const body = await readBody(request);
    const targets = await saveTargets(db, teamId, body.targets ?? body);
    await logEvent(db, { action: "targets_saved", detail: targets, actor });
    return json({ ok: true, targets });
  }

  // ── reports ──────────────────────────────────────────────────────────────
  if (sub === "/reports" && request.method === "GET") {
    const kind = url.searchParams.get("kind");
    const limit = Math.min(Math.max(Number(url.searchParams.get("limit") ?? 30) || 30, 1), 200);
    const where = ["team_id = ?"];
    const binds: any[] = [teamId];
    if (kind && KINDS.includes(kind as Kind)) {
      where.push("kind = ?");
      binds.push(kind);
    }
    const sup = url.searchParams.get("supervisor");
    if (sup) {
      where.push("LOWER(COALESCE(supervisor,'')) = LOWER(?)");
      binds.push(sup);
    }
    const from = url.searchParams.get("from");
    if (from) {
      where.push("period_key >= ?");
      binds.push(from);
    }
    const rows = (
      await db
        .prepare(
          `SELECT id, kind, period_key, shift, supervisor, owner_email, status, submitted_at, submitted_by, published, created_at, updated_at
           FROM qa_sup_reports WHERE ${where.join(" AND ")} ORDER BY period_key DESC, id DESC LIMIT ${limit}`
        )
        .bind(...binds)
        .all<any>()
    ).results.map((r: any) => ({ ...r, published: parse(r.published, []) }));
    return json({ team_id: teamId, reports: rows });
  }

  if (sub === "/reports" && request.method === "POST") {
    const body = await readBody(request);
    const kind = String(body.kind ?? "") as Kind;
    if (!KINDS.includes(kind)) return json({ detail: `kind must be one of ${KINDS.join(", ")}` }, 400);
    let key = String(body.period_key ?? "").trim();
    if (!key) key = kind === "daily" ? today : kind === "weekly" ? isoWeekKey(today) : kind === "monthly" ? today.slice(0, 7) : nextWeekday(today, ctx.cfg.meeting_weekday);
    if (!validKey(kind, key)) return json({ detail: `bad period_key for ${kind}: ${key}` }, 400);
    let shift: string | null = null;
    if (kind === "daily" && body.shift) {
      if (!ctx.shifts.some((s) => s.key === body.shift)) return json({ detail: `unknown shift ${body.shift}` }, 400);
      shift = String(body.shift);
    }
    let supervisor: string | null = null;
    if (kind === "weekly" || kind === "monthly") {
      supervisor = body.supervisor === undefined ? await labelForEmail(db, teamId, actor) : body.supervisor ? String(body.supervisor).trim() : null;
    }
    await db
      .prepare(
        "INSERT INTO qa_sup_reports (team_id, kind, period_key, shift, supervisor, owner_email) VALUES (?,?,?,?,?,?) " +
          "ON CONFLICT(team_id, kind, period_key, COALESCE(shift,''), COALESCE(supervisor,'')) DO NOTHING"
      )
      .bind(teamId, kind, key, shift, supervisor, actor || "?")
      .run();
    const row = await db
      .prepare("SELECT * FROM qa_sup_reports WHERE team_id = ? AND kind = ? AND period_key = ? AND COALESCE(shift,'') = ? AND COALESCE(supervisor,'') = ?")
      .bind(teamId, kind, key, shift ?? "", supervisor ?? "")
      .first<any>();
    const created = row.owner_email === (actor || "?") && row.created_at === row.updated_at;
    if (created) await logEvent(db, { report_id: row.id, action: "created", detail: { kind, key, shift, supervisor }, actor });
    return json({ id: row.id, created, kind, period_key: key, shift, supervisor, url: `/supervisor/${teamId}/${kind === "biweekly" ? `tracker/${key}` : `report/${row.id}`}` });
  }

  m = sub.match(/^\/reports\/(\d+)(\/[a-z-]+)?$/);
  if (m) {
    const id = Number(m[1]);
    const action = m[2] ?? "";
    const raw = await getReport(db, teamId, id);
    if (!raw) return json({ detail: "report not found" }, 404);
    const row = rowOf(raw);

    if (!action && request.method === "GET") {
      const force = url.searchParams.get("recompute") === "1";
      const facts = await factsFor(db, ctx, row, today, force);
      if (row.kind === "weekly" && facts?.productivity?.status === "pending") await kickTicker(db, request, "sup_pull");
      return json({
        ...row,
        auto: facts,
        published: parse(raw.published, []),
        spec: FIELD_SPEC[row.kind],
        targets: ctx.cfg.targets,
        period: periodRange(row.kind, row.period_key),
        team: ctx.name,
        timezone: ctx.tz,
        today,
        events: (await db.prepare("SELECT action, actor_email, at, detail FROM qa_sup_events WHERE report_id = ? ORDER BY at DESC LIMIT 20").bind(id).all<any>()).results,
      });
    }

    if (!action && request.method === "PUT") {
      const body = await readBody(request);
      const manual = body.manual && typeof body.manual === "object" ? body.manual : null;
      if (!manual) return json({ detail: "body.manual object required" }, 400);
      const at = nowIso();
      await db.prepare("UPDATE qa_sup_reports SET manual = ?, updated_at = ? WHERE id = ?").bind(JSON.stringify(manual), at, id).run();
      await logEvent(db, { report_id: id, action: "saved", detail: { fields: Object.keys(manual).length }, actor });
      return json({ ok: true, id, updated_at: at });
    }

    if (action === "/submit" && request.method === "POST") {
      const body = await readBody(request);
      const manual = body.manual && typeof body.manual === "object" ? body.manual : row.manual;
      const facts = await factsFor(db, ctx, { ...row, manual, status: "draft" }, today, true);
      const at = nowIso();
      await db
        .prepare("UPDATE qa_sup_reports SET manual = ?, auto = ?, status = 'submitted', submitted_at = ?, submitted_by = ?, updated_at = ? WHERE id = ?")
        .bind(JSON.stringify(manual), JSON.stringify(facts), at, actor, at, id)
        .run();
      await logEvent(db, { report_id: id, action: "submitted", actor });
      return json({ ok: true, id, status: "submitted", submitted_at: at });
    }

    if (action === "/reopen" && request.method === "POST") {
      await db.prepare("UPDATE qa_sup_reports SET status = 'draft', updated_at = ? WHERE id = ?").bind(nowIso(), id).run();
      await logEvent(db, { report_id: id, action: "reopened", actor });
      return json({ ok: true, id, status: "draft" });
    }

    if (action === "/live" && request.method === "POST") {
      if (row.kind !== "daily") return json({ detail: "live sections exist on daily reports only" }, 400);
      const live = await fetchDailyLive(ctx, row.period_key, row.shift, env);
      const auto = row.status === "submitted" && row.auto ? { ...row.auto, ...live, live } : { live_only: true, live };
      await db.prepare("UPDATE qa_sup_reports SET auto = ?, updated_at = ? WHERE id = ?").bind(JSON.stringify(auto), nowIso(), id).run();
      await logEvent(db, { report_id: id, action: "live_fetched", detail: { tickets: live.tickets.status, slack: live.slack.status }, actor });
      return json({ ok: true, ...live });
    }

    if (action === "/refresh-pull" && request.method === "POST") {
      if (row.kind !== "weekly") return json({ detail: "pulls belong to weekly reviews" }, 400);
      const { start, end } = weekRange(row.period_key);
      const pull = await ensurePull(db, teamId, start, end, ctx.tz);
      if (!pull?.id) return json({ ok: false, note: pull?.note ?? "no pull" });
      await resetPull(db, pull.id);
      const ticker = await kickTicker(db, request, "sup_pull_refresh");
      await logEvent(db, { report_id: id, action: "pull_refreshed", detail: { pull_id: pull.id, ticker }, actor });
      return json({ ok: true, pull_id: pull.id, ticker });
    }

    if (action === "/publish" && request.method === "POST") {
      const body = await readBody(request);
      const target = String(body.target ?? "");
      if (!["slack", "sheet"].includes(target)) return json({ detail: "target must be slack or sheet" }, 400);
      const facts = await factsFor(db, ctx, row, today);
      const full: ReportRow = { ...row, auto: facts };
      let tracker: any = undefined;
      if (row.kind === "biweekly") tracker = buildTracker(facts, row.manual);
      const rendered = renderReport(full, ctx.name, { tracker });
      let result: any;
      if (target === "slack") {
        const { postMessage } = await import("../lib/slack.js");
        const posted = await postMessage(env.SLACK_BOT_TOKEN, ctx.cfg.slack_channel ?? "", rendered.text.slice(0, 3000), rendered.blocks);
        if ("error" in posted) return json({ ok: false, target, error: posted.error }, 502);
        result = { target, at: nowIso(), by: actor, ts: posted.ts, channel: posted.channel, permalink: posted.permalink ?? null };
      } else {
        if (!env.GSHEETS_SA_JSON) return json({ ok: false, target, error: "no sheets credentials (GSHEETS_SA_JSON)" }, 502);
        if (!ctx.spreadsheetId) return json({ ok: false, target, error: "eod_sheet.spreadsheet_id not configured" }, 502);
        const { openSpreadsheet, upsertTab } = await import("../lib/googleSheets.js");
        const tab = `${ctx.cfg.sheet_tab_prefix}_${row.kind[0].toUpperCase()}${row.kind.slice(1)}`;
        try {
          const client = await openSpreadsheet(env.GSHEETS_SA_JSON, ctx.spreadsheetId, fetch, Date.now());
          const header = SHEET_HEADERS[row.kind];
          const r = sheetRow(full, rendered, tracker);
          const n = await upsertTab(client, tab, header, [r], new Set([instanceKey(full)]), (x) => String(x[0]));
          result = { target, at: nowIso(), by: actor, tab, rows: n, url: `https://docs.google.com/spreadsheets/d/${ctx.spreadsheetId}/edit` };
        } catch (err) {
          return json({ ok: false, target, error: String((err as any)?.message ?? err).slice(0, 200) }, 502);
        }
      }
      const published = parse(raw.published, []);
      published.push(result);
      await db.prepare("UPDATE qa_sup_reports SET published = ?, updated_at = ? WHERE id = ?").bind(JSON.stringify(published), nowIso(), id).run();
      await logEvent(db, { report_id: id, action: "published", detail: result, actor });
      return json({ ok: true, ...result, published });
    }

    if (action === "/preview" && request.method === "GET") {
      const facts = await factsFor(db, ctx, row, today);
      const full: ReportRow = { ...row, auto: facts };
      const tracker = row.kind === "biweekly" ? buildTracker(facts, row.manual) : undefined;
      return json(renderReport(full, ctx.name, { tracker }));
    }
    return json({ detail: "unknown report action" }, 404);
  }

  // ── tracker (biweekly) ────────────────────────────────────────────────────
  m = sub.match(/^\/tracker\/(\d{4}-\d{2}-\d{2})$/);
  if (m && request.method === "GET") {
    const date = m[1];
    if (!validKey("biweekly", date)) return json({ detail: "bad date" }, 400);
    await db
      .prepare("INSERT INTO qa_sup_reports (team_id, kind, period_key, owner_email) VALUES (?,'biweekly',?,?) ON CONFLICT(team_id, kind, period_key, COALESCE(shift,''), COALESCE(supervisor,'')) DO NOTHING")
      .bind(teamId, date, actor || "?")
      .run();
    const raw = await db
      .prepare("SELECT * FROM qa_sup_reports WHERE team_id = ? AND kind = 'biweekly' AND period_key = ? AND shift IS NULL AND supervisor IS NULL")
      .bind(teamId, date)
      .first<any>();
    const row = rowOf(raw);
    const facts = await factsFor(db, ctx, row, today);
    if (facts?.weekly?.productivity?.status === "pending") await kickTicker(db, request, "sup_pull");
    const tracker = buildTracker(facts, row.manual);
    return json({
      report_id: row.id, status: row.status, owner_email: row.owner_email, submitted_at: row.submitted_at,
      published: parse(raw.published, []), manual: row.manual, auto: facts, tracker, spec: FIELD_SPEC.biweekly,
      team: ctx.name, today, targets: ctx.cfg.targets,
      previous: (await db.prepare("SELECT period_key, status FROM qa_sup_reports WHERE team_id = ? AND kind = 'biweekly' AND period_key < ? ORDER BY period_key DESC LIMIT 6").bind(teamId, date).all<any>()).results,
    });
  }

  // ── action plans (§6) ────────────────────────────────────────────────────
  if (sub === "/action-plans" && request.method === "GET") {
    const status = (url.searchParams.get("status") ?? "open") as "open" | "closed" | "all";
    const plans = await listActionPlans(db, teamId, { status: ["open", "closed", "all"].includes(status) ? status : "open", supervisor: url.searchParams.get("supervisor"), limit: 300 });
    return json({ team_id: teamId, plans, areas: PLAN_AREAS, statuses: PLAN_STATUSES });
  }
  if (sub === "/action-plans" && request.method === "POST") {
    const b = await readBody(request);
    const area = String(b.area ?? "other");
    if (!PLAN_AREAS.includes(area as any)) return json({ detail: `area must be one of ${PLAN_AREAS.join(", ")}` }, 400);
    const issue = String(b.issue ?? "").trim();
    const action = String(b.action ?? "").trim();
    if (!issue || !action) return json({ detail: "issue and action are required" }, 400);
    const owner = String(b.owner ?? "").trim() || (await labelForEmail(db, teamId, actor)) || actor;
    let agentId: number | null = null;
    let agentName: string | null = b.agent_name ? String(b.agent_name).trim() : null;
    if (b.agent_id) {
      const ag = await db.prepare("SELECT id, name, canonical_name FROM qa_agents WHERE id = ? AND team_id = ?").bind(Number(b.agent_id), teamId).first<any>();
      if (ag) {
        agentId = ag.id;
        agentName = agentName || ag.canonical_name || ag.name;
      }
    }
    const res = await db
      .prepare(
        "INSERT INTO qa_sup_action_plans (team_id, area, agent_id, agent_name, issue, cause, action, owner, follow_up_date, source_report_id, created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?)"
      )
      .bind(teamId, area, agentId, agentName, issue, b.cause ? String(b.cause) : null, action, owner, b.follow_up_date ? String(b.follow_up_date) : null, b.source_report_id ? Number(b.source_report_id) : null, actor || "?")
      .run();
    const id = Number(res.meta.last_row_id);
    await logEvent(db, { plan_id: id, report_id: b.source_report_id ? Number(b.source_report_id) : null, action: "plan_created", detail: { area, issue }, actor });
    return json({ ok: true, id }, 201);
  }
  m = sub.match(/^\/action-plans\/(\d+)$/);
  if (m && request.method === "PATCH") {
    const id = Number(m[1]);
    const cur = await db.prepare("SELECT * FROM qa_sup_action_plans WHERE id = ? AND team_id = ?").bind(id, teamId).first<any>();
    if (!cur) return json({ detail: "plan not found" }, 404);
    const b = await readBody(request);
    const sets: string[] = [];
    const binds: any[] = [];
    for (const k of ["issue", "cause", "action", "owner", "follow_up_date", "result", "agent_name", "area"]) {
      if (!(k in b)) continue;
      if (k === "area" && !PLAN_AREAS.includes(b.area)) return json({ detail: "bad area" }, 400);
      sets.push(`${k} = ?`);
      binds.push(b[k] === null || b[k] === "" ? null : String(b[k]));
    }
    if ("status" in b) {
      if (!PLAN_STATUSES.includes(b.status)) return json({ detail: "bad status" }, 400);
      sets.push("status = ?");
      binds.push(b.status);
      sets.push("closed_at = ?");
      binds.push(b.status === "done" || b.status === "dropped" ? nowIso() : null);
    }
    if (!sets.length) return json({ detail: "nothing to update" }, 400);
    sets.push("updated_at = ?");
    binds.push(nowIso());
    await db.prepare(`UPDATE qa_sup_action_plans SET ${sets.join(", ")} WHERE id = ?`).bind(...binds, id).run();
    await logEvent(db, { plan_id: id, action: "plan_updated", detail: b, actor });
    return json({ ok: true, id });
  }

  return json({ detail: "unknown supervisor endpoint" }, 404);
}

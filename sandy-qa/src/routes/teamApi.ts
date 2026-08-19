// Worker routes for the ported QA pages + team analytics APIs.
// Pages are the Railway originals served verbatim (?raw imports) — visual
// parity by construction; the APIs they consume are implemented here against
// D1 (PortManifest §4/§5). SSE is the §6.1 D1-as-bus design.

import { loadTeamConfig } from "../lib/teamConfig.js";
import { fetchHistoryFrame } from "../lib/historyFrame.js";
import { assembleTeamEvals, assembleTeamStats } from "../lib/teamStats.js";
import {
  fetchRecords,
  getByEvalId,
  listAgents,
  resolveTeamForAgent,
} from "../lib/records.js";
// @ts-ignore vite ?raw
import teamDashboardHtml from "../../pages/team_dashboard.html?raw";
// @ts-ignore vite ?raw
import teamEvalsHtml from "../../pages/team_evals.html?raw";
// @ts-ignore vite ?raw
import agentDashboardHtml from "../../pages/dashboard.html?raw";
// @ts-ignore vite ?raw
import datapointHtml from "../../pages/datapoint.html?raw";
// @ts-ignore vite ?raw
import lookupHtml from "../../pages/lookup.html?raw";
// @ts-ignore vite ?raw
import lookupRetellHtml from "../../pages/lookup_retell.html?raw";
// @ts-ignore vite ?raw
import onepagerShellHtml from "../../pages/onepager.html?raw";
// @ts-ignore vite ?raw
import scorecardHtml from "../../pages/scorecard.html?raw";
// @ts-ignore vite ?raw
import scoringConsoleHtml from "../../pages/index.html?raw";
// @ts-ignore vite ?raw
import adminHtml from "../../pages/admin.html?raw";
// @ts-ignore vite ?raw
import coachingPageHtml from "../../pages/coaching.html?raw";
// @ts-ignore vite ?raw
import headerCss from "../../pages/static/header.css?raw";
// @ts-ignore vite ?raw
import headerJs from "../../pages/static/header.js?raw";
import { accessEmail, canCoach, resolveAccess, selfAgentFor, type Access } from "../lib/rbac.js";

const RAILWAY_BASE = "https://hellolanding-qa.up.railway.app";
const KNOWN_TEAMS = new Set(["member_support", "sales", "sofia"]);

const GREETING_PAGE = `<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>QA Scoring — Landing</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=Fraunces:opsz,wght@9..144,400;9..144,600;9..144,700&display=swap" rel="stylesheet">
<style>
:root{--navy:#15192D;--accent:#1A61D9;--bg:#E7EFFB;--surface:#fff;--border:#c9d5e8;--text:#15192D;--muted:#5a6478;--green:#28A745}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'DM Mono',monospace;background:var(--bg);color:var(--text);min-height:100vh}
h1,h2,h3{font-family:'Fraunces',serif}
.header{background:var(--navy);color:#fff;padding:20px 24px}
.header h1{font-size:1.35rem;font-weight:700}
.header .sub{font-size:.78rem;color:rgba(255,255,255,.65);margin-top:4px}
.container{max-width:960px;margin:0 auto;padding:28px 24px;display:flex;flex-direction:column;gap:22px}
.teams{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:16px}
.team-card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:22px;
  text-decoration:none;color:inherit;display:flex;flex-direction:column;gap:8px;transition:border-color .15s,transform .15s}
.team-card:hover{border-color:var(--accent);transform:translateY(-2px)}
.team-card h2{font-size:1.15rem}
.team-card .go{color:var(--accent);font-size:.8rem;margin-top:6px}
.team-card.soon{opacity:.65;cursor:default}
.team-card.soon:hover{border-color:var(--border);transform:none}
.badge{display:inline-block;padding:2px 10px;border-radius:9999px;font-size:.65rem;font-weight:500;
  text-transform:uppercase;letter-spacing:.05em;background:#dbeafe;color:#1e40af;width:fit-content}
.badge.soon-b{background:#fef3c7;color:#92400e}
.card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:22px}
.card h3{font-size:1rem;margin-bottom:10px}
.how li{margin:8px 0 8px 18px;font-size:.82rem;line-height:1.5}
.how b{font-weight:600}
.how .path{color:var(--accent)}
.note{font-size:.72rem;color:var(--muted);line-height:1.5}
</style></head><body>
<div class="header"><h1>QA Scoring</h1>
<div class="sub">Landing call-quality platform — two-stage AI scoring, analyst review, team analytics</div></div>
<div class="container">
  <div class="teams">
    <a class="team-card" href="/dashboard/member_support">
      <span class="badge">Live</span><h2>Member Support</h2>
      <span class="note">Team dashboard, agent analytics, month drill-downs</span>
      <span class="go">Open dashboard &rsaquo;</span></a>
    <a class="team-card" href="/dashboard/sales">
      <span class="badge">Live</span><h2>Sales</h2>
      <span class="note">Team dashboard, agent analytics, month drill-downs</span>
      <span class="go">Open dashboard &rsaquo;</span></a>
    <a class="team-card" href="/dashboard/sofia">
      <span class="badge">Live</span><h2>Sofia AI</h2>
      <span class="note">QA for our voice AI agent — calls on Retell.ai</span>
      <span class="go">Open dashboard &rsaquo;</span></a>
  </div>
  <div class="card"><h3>Finding your way around</h3><ul class="how">
    <li><b>Team dashboard</b> — <span class="path">/dashboard/&lt;team&gt;</span>: live KPIs, SPC + score
      distribution, EWMA by agent (click any bar to drill down). Updates push live — no refresh needed.</li>
    <li><b>Month drill-down</b> — click the Last/Current Month chiclets for every evaluation in that month.</li>
    <li><b>Agent view</b> — click any agent name for their history and section trends.</li>
    <li><b>Datapoint</b> — every score links to the full scorecard: section scores, confidence, reasoning.</li>
    <li><b>Call lookup</b> — <span class="path">/lookup/&lt;team&gt;</span>: call history + recordings
      (Dialpad; Sofia&rsquo;s is Retell-backed with one-click scoring). <b>Restricted</b> to authorized QA staff.</li>
    <li><b>Scoring console</b> — <span class="path">/score/&lt;team&gt;</span>: batch-score calls by
      Dialpad ID + the human-review queue. <b>Restricted</b> to authorized QA staff.</li>
    <li><b>Coaching</b> — <span class="path">/coaching/&lt;team&gt;</span>: 1:1 sessions, agent
      commitments, and the confirmation queue. <b>Restricted</b> to QA staff and team managers.</li>
  </ul></div>
  <div class="note">During the migration shadow period this app mirrors production evaluation
  data live; AI scoring &amp; review actions are live here for QA staff via the scoring console.</div>
</div></body></html>`;

const html = (body: string) =>
  new Response(body, { headers: { "Content-Type": "text/html; charset=utf-8" } });
const json = (data: unknown, status = 200) =>
  new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json" },
  });

// RBAC (lib/rbac.ts): roles resolve automatically from the SSO email —
// users discover restrictions only by hitting one, and the denial page
// offers a self-service access request. LOOKUP_ALLOW remains a grandfather
// bridge inside resolveAccess until the secret is deleted.

// Denial page with the request-access flow. The person never needed to
// know RBAC existed until this moment — the page explains the wall and
// lets them request through it in place.
function deniedPage(kind: "lookup" | "score" | "coaching", teamId: string): Response {
  const copy =
    kind === "lookup"
      ? {
          title: "Call lookup is restricted",
          why: "This page surfaces call history and recordings across the company, so it is limited to authorized QA staff.",
        }
      : kind === "coaching"
        ? {
            title: "The coaching page is restricted",
            why: "Coaching sessions carry manager feedback and agent commitments, so this page is limited to QA staff and team managers.",
          }
        : {
          title: "The scoring console is restricted",
          why: "Triggering AI evaluations creates progression records for agents, so it is limited to authorized QA staff.",
        };
  const body = `<!DOCTYPE html><html><head><title>${copy.title}</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&display=swap" rel="stylesheet"></head>
<body style="font-family:'DM Mono',monospace;background:#E7EFFB;color:#15192D;display:grid;place-items:center;min-height:100vh;margin:0">
<div style="background:#fff;border:1px solid #c9d5e8;border-radius:12px;padding:32px;max-width:560px">
<h2 style="margin-top:0">${copy.title}</h2>
<p style="font-size:.85rem;line-height:1.55">${copy.why}</p>
<div id="req" style="margin-top:20px">
  <textarea id="req-note" placeholder="Optional: why do you need access?"
    style="width:100%;font-family:inherit;font-size:.8rem;padding:10px;border:1px solid #c9d5e8;border-radius:6px;background:#E7EFFB;min-height:64px"></textarea>
  <button id="req-btn" onclick="requestAccess()"
    style="margin-top:10px;font-family:inherit;font-size:.82rem;padding:10px 20px;border:1px solid #1A61D9;border-radius:6px;background:#1A61D9;color:#fff;cursor:pointer">
    Request access</button>
  <span id="req-status" style="display:block;margin-top:10px;font-size:.75rem;color:#5a6478"></span>
</div>
<p style="color:#5a6478;font-size:.72rem;margin-top:18px"><a href="/" style="color:#1A61D9">&larr; Back to dashboards</a></p>
</div>
<script>
async function requestAccess() {
  const btn = document.getElementById('req-btn');
  const status = document.getElementById('req-status');
  btn.disabled = true;
  try {
    const res = await fetch('/api/access-request', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ page: '${kind}', team_id: '${teamId}',
        note: document.getElementById('req-note').value.trim() }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || ('HTTP ' + res.status));
    status.textContent = data.deduped
      ? 'You already have a pending request — the QA team will review it.'
      : 'Request sent — the QA team will review it.';
    btn.style.display = 'none';
    document.getElementById('req-note').style.display = 'none';
  } catch (e) {
    status.textContent = 'Request failed: ' + e.message;
    btn.disabled = false;
  }
}
</script></body></html>`;
  return new Response(body, {
    status: 403,
    headers: { "Content-Type": "text/html; charset=utf-8" },
  });
}

export async function handleTeamRoutes(
  request: Request,
  db: D1Database,
  url: URL,
  dialpadKey?: string,
  lookupAllow?: string,
  pulpo?: { url?: string; token?: string },
  gasUrls?: { member_support?: string; sales?: string; sofia?: string },
  retellKey?: string
): Promise<Response | null> {
  const path = url.pathname;

  // ── greeting page (platform default URL) ─────────────────────────────────
  if (path === "/") return html(GREETING_PAGE);

  // ── static assets shared by the ported pages ──────────────────────────────
  if (path === "/static/header.css")
    return new Response(headerCss, { headers: { "Content-Type": "text/css" } });
  if (path === "/static/header.js")
    return new Response(headerJs, {
      headers: { "Content-Type": "text/javascript" },
    });

  // ── pages ────────────────────────────────────────────────────────────────
  let m = path.match(/^\/dashboard\/([^/]+)\/evals$/);
  if (m && KNOWN_TEAMS.has(m[1])) return html(teamEvalsHtml);
  m = path.match(/^\/dashboard\/([^/]+)$/);
  if (m && KNOWN_TEAMS.has(m[1])) return html(teamDashboardHtml);

  // Interim: datapoint + per-agent dashboard port in the next slice. Keep
  // the link graph honest — pages exist, explain themselves, link to Railway.
  const interimPage = (title: string, target: string) =>
    html(
      `<!DOCTYPE html><html><head><title>${title} — porting</title></head>
<body style="font-family:monospace;background:#E7EFFB;color:#15192D;display:grid;place-items:center;min-height:100vh;margin:0">
<div style="background:#fff;border:1px solid #c9d5e8;border-radius:12px;padding:32px;max-width:520px">
<h2 style="margin-top:0">${title}: next port slice</h2>
<p>This page hasn't moved to Sandy yet (strangler order: dashboards → stats → drill-downs).</p>
<p><a href="${target}" style="color:#1A61D9">Open on Railway &rsaquo;</a></p>
<p><a href="javascript:history.back()" style="color:#5a6478">&larr; Back</a></p>
</div></body></html>`
    );
  // Real pages (Railway originals, SSO shim) — datapoint, per-agent
  // dashboard, and lookup. The one-pager stays interim (renderer +
  // EOM-export coupling ports in a later slice).
  m = path.match(/^\/datapoint\/([^/]+)\/([^/]+)$/);
  if (m && KNOWN_TEAMS.has(m[1])) return html(datapointHtml);
  m = path.match(/^\/datapoint\/([^/]+)$/);
  if (m) return html(datapointHtml); // legacy no-team route (defaults MS)
  m = path.match(/^\/dashboard\/([^/]+)\/onepager\/(.+)$/);
  if (m && KNOWN_TEAMS.has(m[1])) return html(onepagerShellHtml);
  m = path.match(/^\/dashboard\/([^/]+)\/agent\/(.+)$/);
  if (m && KNOWN_TEAMS.has(m[1])) return html(agentDashboardHtml);

  // Scorecard editor (§4.1/§4.3 + flagged-review console) — the Railway
  // page served verbatim; it renders any evaluation via GET /score/{id}.
  m = path.match(/^\/scorecard\/([^/]+)\/([^/]+)$/);
  if (m && KNOWN_TEAMS.has(m[1])) return html(scorecardHtml);
  m = path.match(/^\/lookup\/([^/]+)$/);
  if (m && KNOWN_TEAMS.has(m[1])) {
    // sofia's lookup is Retell-backed (list-calls v3 — SofiaRetellSpec §8 R3,
    // the discovery surface: Retell call ids aren't knowable a priori).
    const access = await resolveAccess(request, db, lookupAllow);
    if (!access.privileged) return deniedPage("lookup", m[1]);
    return html(m[1] === "sofia" ? lookupRetellHtml : lookupHtml);
  }

  // Scoring console (index.html port) — batch score-by-call-ID + the §0.3
  // review queue. Privileged (admin|qa): scoring creates progression records.
  m = path.match(/^\/score\/([^/]+)$/);
  if (m && KNOWN_TEAMS.has(m[1])) {
    const access = await resolveAccess(request, db, lookupAllow);
    return access.privileged ? html(scoringConsoleHtml) : deniedPage("score", m[1]);
  }

  // Coaching page (§6 — dedicated, never mixed with the review queue).
  m = path.match(/^\/coaching\/([^/]+)$/);
  if (m && KNOWN_TEAMS.has(m[1])) {
    const access = await resolveAccess(request, db, lookupAllow);
    return canCoach(access, m[1]) ? html(coachingPageHtml) : deniedPage("coaching", m[1]);
  }

  // ── access admin (roles + request inbox) ─────────────────────────────────
  if (path === "/admin") {
    const access = await resolveAccess(request, db, lookupAllow);
    if (access.role !== "admin")
      return new Response("Admins only.", { status: 403 });
    return html(adminHtml);
  }
  if (path === "/api/admin/overview" && request.method === "GET") {
    const access = await resolveAccess(request, db, lookupAllow);
    if (access.role !== "admin") return json({ detail: "Admins only." }, 403);
    const roles = await db
      .prepare("SELECT email, role, team_id, granted_by, granted_at, note FROM qa_roles ORDER BY role, email")
      .all<any>();
    const requests = await db
      .prepare("SELECT id, email, page, team_id, note, created_at FROM qa_access_requests WHERE status = 'pending' ORDER BY created_at")
      .all<any>();
    return json({ roles: roles.results, requests: requests.results, me: access.email });
  }
  if (path === "/api/admin/roles" && request.method === "POST") {
    const access = await resolveAccess(request, db, lookupAllow);
    if (access.role !== "admin") return json({ detail: "Admins only." }, 403);
    let body: any = {};
    try { body = await request.json(); } catch { return json({ detail: "JSON body required" }, 422); }
    const email = (body.email ?? "").trim().toLowerCase();
    if (!email.includes("@")) return json({ detail: "valid email required" }, 422);
    if (body.action === "remove") {
      if (email === access.email)
        return json({ detail: "You can't remove your own admin role (lockout guard)." }, 422);
      await db.prepare("DELETE FROM qa_roles WHERE email = ?").bind(email).run();
      return json({ ok: true, removed: email });
    }
    const role = body.role;
    if (!["admin", "qa", "manager", "viewer"].includes(role))
      return json({ detail: "role must be admin|qa|manager|viewer" }, 422);
    if (email === access.email && role !== "admin")
      return json({ detail: "You can't demote your own admin role (lockout guard)." }, 422);
    await db
      .prepare(
        `INSERT INTO qa_roles (email, role, team_id, granted_by, note) VALUES (?,?,?,?,?)
         ON CONFLICT(email) DO UPDATE SET role=excluded.role, team_id=excluded.team_id,
           granted_by=excluded.granted_by, granted_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'),
           note=excluded.note`
      )
      .bind(email, role, body.team_id ?? null, access.email, body.note ?? null)
      .run();
    return json({ ok: true, email, role });
  }
  m = path.match(/^\/api\/admin\/access-requests\/(\d+)$/);
  if (m && request.method === "POST") {
    const access = await resolveAccess(request, db, lookupAllow);
    if (access.role !== "admin") return json({ detail: "Admins only." }, 403);
    let body: any = {};
    try { body = await request.json(); } catch { return json({ detail: "JSON body required" }, 422); }
    const reqRow = await db
      .prepare("SELECT id, email, page, status FROM qa_access_requests WHERE id = ?")
      .bind(Number(m[1]))
      .first<any>();
    if (!reqRow) return json({ detail: "request not found" }, 404);
    if (reqRow.status !== "pending") return json({ detail: `already ${reqRow.status}` }, 409);
    const action = body.action;
    if (!["approve", "deny"].includes(action))
      return json({ detail: "action must be approve|deny" }, 422);
    if (action === "approve") {
      const role = ["admin", "qa"].includes(body.role) ? body.role : "qa";
      await db
        .prepare(
          `INSERT INTO qa_roles (email, role, granted_by, note) VALUES (?,?,?,?)
           ON CONFLICT(email) DO UPDATE SET role=excluded.role,
             granted_by=excluded.granted_by, granted_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'),
             note=excluded.note`
        )
        .bind(reqRow.email, role, access.email, `via access request #${reqRow.id} (${reqRow.page})`)
        .run();
    }
    await db
      .prepare(
        "UPDATE qa_access_requests SET status = ?, resolved_by = ?, resolved_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?"
      )
      .bind(action === "approve" ? "approved" : "denied", access.email, reqRow.id)
      .run();
    return json({ ok: true, id: reqRow.id, status: action === "approve" ? "approved" : "denied" });
  }

  // Self-service access request (any SSO-authenticated employee) — the
  // denial pages POST here. /admin is the inbox.
  if (path === "/api/access-request" && request.method === "POST") {
    const email = accessEmail(request);
    if (!email) return json({ detail: "No SSO identity on this request." }, 401);
    let body: any = {};
    try { body = await request.json(); } catch { return json({ detail: "JSON body required" }, 422); }
    const page = ["score", "coaching"].includes(body.page) ? body.page : "lookup";
    const pending = await db
      .prepare(
        "SELECT id FROM qa_access_requests WHERE email = ? AND page = ? AND status = 'pending' LIMIT 1"
      )
      .bind(email, page)
      .first<any>();
    if (pending) return json({ ok: true, deduped: true });
    await db
      .prepare(
        "INSERT INTO qa_access_requests (email, page, team_id, note) VALUES (?,?,?,?)"
      )
      .bind(email, page, body.team_id ?? null, (body.note ?? "").toString().slice(0, 500) || null)
      .run();
    // Slack notification seam — deliberately NOT built yet (the Slack
    // integration gets its own design pass rather than a rushed webhook).
    // When it lands: notify the QA admin channel/DM here with the request
    // row; until then /admin's pending badge is the inbox.
    return json({ ok: true });
  }

  // ── scoring trigger + status (scoring.ts) ────────────────────────────────
  // One shared privileged gate for every mutating scoring action — the
  // pages render buttons by whoami role, but the API is the real wall.
  const requirePrivileged = async (): Promise<Response | null> => {
    const access = await resolveAccess(request, db, lookupAllow);
    return access.privileged
      ? null
      : json({ detail: "This action is restricted to QA staff — request access from the Lookup or Scoring page." }, 403);
  };
  m = path.match(/^\/api\/([^/]+)\/review-queue$/);
  if (m && KNOWN_TEAMS.has(m[1]) && request.method === "GET") {
    const { reviewQueue } = await import("./scoring.js");
    return reviewQueue(db, m[1]);
  }

  // ── coaching loop (CoachingLoopSpec §4 — routes/coaching.ts) ─────────────
  // Gates live inside the module: `coach` capability (admin|qa|scoped
  // manager) on everything, plus the §4.5 redacted self-view on the GET.
  m = path.match(/^\/api\/([^/]+)\/agents\/([^/]+)\/coachings$/);
  if (m && KNOWN_TEAMS.has(m[1]) && request.method === "GET") {
    const { listAgentCoachings } = await import("./coaching.js");
    return listAgentCoachings(request, db, m[1], decodeURIComponent(m[2]), url, lookupAllow);
  }
  m = path.match(/^\/api\/([^/]+)\/coachings$/);
  if (m && KNOWN_TEAMS.has(m[1]) && request.method === "POST") {
    const { createCoaching } = await import("./coaching.js");
    return createCoaching(request, db, m[1], lookupAllow);
  }
  if (m && KNOWN_TEAMS.has(m[1]) && request.method === "GET") {
    const { listTeamCoachings } = await import("./coaching.js");
    return listTeamCoachings(request, db, m[1], url, lookupAllow);
  }
  m = path.match(/^\/api\/([^/]+)\/coachings\/(\d+)$/);
  if (m && KNOWN_TEAMS.has(m[1]) && request.method === "PATCH") {
    const { patchCoaching } = await import("./coaching.js");
    return patchCoaching(request, db, m[1], Number(m[2]), lookupAllow);
  }
  m = path.match(/^\/api\/([^/]+)\/coachings\/(\d+)\/(conduct|cancel|confirm)$/);
  if (m && KNOWN_TEAMS.has(m[1]) && request.method === "POST") {
    const mod = await import("./coaching.js");
    const fn =
      m[3] === "conduct" ? mod.conductCoaching
      : m[3] === "cancel" ? mod.cancelCoaching
      : mod.confirmCoaching;
    return fn(request, db, m[1], Number(m[2]), lookupAllow);
  }
  m = path.match(/^\/api\/([^/]+)\/coaching-queue$/);
  if (m && KNOWN_TEAMS.has(m[1]) && request.method === "GET") {
    const { coachingQueue } = await import("./coaching.js");
    return coachingQueue(request, db, m[1], lookupAllow);
  }
  m = path.match(/^\/api\/([^/]+)\/insights\/team$/);
  if (m && KNOWN_TEAMS.has(m[1]) && ["GET", "POST"].includes(request.method)) {
    const { teamInsightRequest } = await import("./insights.js");
    return teamInsightRequest(request, db, m[1], url, lookupAllow);
  }
  m = path.match(/^\/api\/([^/]+)\/chiclets$/);
  if (m && KNOWN_TEAMS.has(m[1]) && request.method === "GET") {
    const { listChiclets } = await import("./coaching.js");
    return listChiclets(db, m[1], url);
  }
  m = path.match(/^\/api\/([^/]+)\/chiclets\/(\d+)\/resolve$/);
  if (m && KNOWN_TEAMS.has(m[1]) && request.method === "POST") {
    const { resolveChiclet } = await import("./coaching.js");
    return resolveChiclet(request, db, m[1], Number(m[2]), lookupAllow);
  }
  m = path.match(/^\/api\/([^/]+)\/score$/);
  if (m && KNOWN_TEAMS.has(m[1]) && request.method === "POST") {
    const deny = await requirePrivileged();
    if (deny) return deny;
    const { scoreTrigger } = await import("./scoring.js");
    return scoreTrigger(request, db, m[1], {
      DIALPAD_API_KEY: dialpadKey,
      RETELL_API_KEY: retellKey,
      PULPO_MCP_URL: pulpo?.url,
      PULPO_MCP_TOKEN: pulpo?.token,
    });
  }
  m = path.match(/^\/api\/([^/]+)\/score\/([^/]+)\/rescore$/);
  if (m && KNOWN_TEAMS.has(m[1]) && request.method === "POST") {
    const deny = await requirePrivileged();
    if (deny) return deny;
    const { rescoreEvaluation } = await import("./scoring.js");
    return rescoreEvaluation(request, db, m[1], decodeURIComponent(m[2]), {
      DIALPAD_API_KEY: dialpadKey,
      RETELL_API_KEY: retellKey,
      PULPO_MCP_URL: pulpo?.url,
      PULPO_MCP_TOKEN: pulpo?.token,
    });
  }
  m = path.match(/^\/api\/([^/]+)\/score\/([^/]+)\/approve$/);
  if (m && KNOWN_TEAMS.has(m[1]) && request.method === "POST") {
    const deny = await requirePrivileged();
    if (deny) return deny;
    const { approveEvaluation } = await import("./scoring.js");
    return approveEvaluation(request, db, m[1], decodeURIComponent(m[2]), {
      editOfFinalized: false,
      gasUrls,
    });
  }
  m = path.match(/^\/api\/([^/]+)\/score\/([^/]+)\/override$/);
  if (m && KNOWN_TEAMS.has(m[1]) && request.method === "POST") {
    const deny = await requirePrivileged();
    if (deny) return deny;
    const { overrideEvaluation } = await import("./scoring.js");
    return overrideEvaluation(request, db, m[1], decodeURIComponent(m[2]), gasUrls);
  }
  m = path.match(/^\/api\/([^/]+)\/datapoints\/([^/]+)\/edit$/);
  if (m && KNOWN_TEAMS.has(m[1]) && request.method === "POST") {
    const deny = await requirePrivileged();
    if (deny) return deny;
    const { approveEvaluation } = await import("./scoring.js");
    return approveEvaluation(request, db, m[1], decodeURIComponent(m[2]), {
      editOfFinalized: true,
      gasUrls,
    });
  }
  m = path.match(/^\/api\/([^/]+)\/score\/([^/]+)$/);
  if (m && KNOWN_TEAMS.has(m[1]) && request.method === "DELETE") {
    const deny = await requirePrivileged();
    if (deny) return deny;
    const { deleteEvaluation } = await import("./scoring.js");
    return deleteEvaluation(request, db, m[1], decodeURIComponent(m[2]), url);
  }
  if (m && KNOWN_TEAMS.has(m[1]) && request.method === "GET") {
    const { scorecardPayload } = await import("./scoring.js");
    return scorecardPayload(db, m[1], decodeURIComponent(m[2]), request);
  }

  // ── drill-down + record APIs ─────────────────────────────────────────────
  m = path.match(/^\/api\/([^/]+)\/datapoints$/);
  if (m && KNOWN_TEAMS.has(m[1])) return datapointsList(db, m[1], url);
  m = path.match(/^\/api\/([^/]+)\/datapoints\/([^/]+)$/);
  if (m && KNOWN_TEAMS.has(m[1])) {
    const config = await loadTeamConfig(db, m[1]);
    const rec = await getByEvalId(db, config, decodeURIComponent(m[2]));
    return rec
      ? json(rec)
      : json({ detail: `No evaluation found for call_id '${m[2]}'.` }, 404);
  }
  m = path.match(/^\/api\/([^/]+)\/agents$/);
  if (m && KNOWN_TEAMS.has(m[1])) return json(await listAgents(db, m[1]));
  m = path.match(/^\/api\/([^/]+)\/agents\/([^/]+)\/history$/);
  if (m && KNOWN_TEAMS.has(m[1]))
    return agentHistory(db, m[1], decodeURIComponent(m[2]), url);
  m = path.match(/^\/api\/([^/]+)\/agents\/([^/]+)\/onepager$/);
  if (m && KNOWN_TEAMS.has(m[1])) {
    const { renderMonthOnepager, lastClosedMonth } = await import("../lib/onepager.js");
    const teamId2 = m[1];
    const agent = decodeURIComponent(m[2]);
    let month = url.searchParams.get("month");
    if (month === null) month = lastClosedMonth();
    else if (!/^\d{4}-(0[1-9]|1[0-2])$/.test(month))
      return json({ detail: "month must be YYYY-MM" }, 422);
    const cfg = await loadTeamConfig(db, teamId2);
    const frame = await fetchHistoryFrame(db, cfg);
    const label = teamId2 === "sales" ? "Sales" : "Member Support";
    const page = await renderMonthOnepager(db, cfg, frame, agent, month, label);
    if (page === null)
      return json({ detail: `No evaluations for '${agent}' in ${month}` }, 404);
    return html(page);
  }
  // Progression assessment (CL4 — CoachingLoopSpec §8): GET serves the
  // persisted current assessment / job status; POST triggers a qa-insights
  // run (idempotent on in-flight jobs; fresh assessments never re-spend).
  m = path.match(/^\/api\/([^/]+)\/agents\/([^/]+)\/progression$/);
  if (m && KNOWN_TEAMS.has(m[1]) && ["GET", "POST"].includes(request.method)) {
    const { progressionRequest } = await import("./insights.js");
    return progressionRequest(request, db, m[1], decodeURIComponent(m[2]), url);
  }
  m = path.match(/^\/api\/([^/]+)\/whoami$/);
  if (m && KNOWN_TEAMS.has(m[1])) {
    // Pages render action buttons off `role` (privileged|team — the
    // Railway contract); rbac_role/email are the richer RBAC identity.
    // can_coach + self_agent (CoachingLoopSpec §4/§4.5): the coaching card
    // and the future agent self-dashboard key off these, never off pages.
    const access = await resolveAccess(request, db, lookupAllow);
    return json({
      role: access.privileged ? "privileged" : "team",
      team_id: m[1],
      rbac_role: access.role,
      email: access.email,
      can_coach: canCoach(access, m[1]),
      self_agent: await selfAgentFor(request, db, m[1]),
    });
  }

  // ── lookup APIs (dialpad teams: Dialpad-backed; sofia: Retell v3 list) ───
  m = path.match(/^\/api\/([^/]+)\/lookup(\/calls|\/recording-link|\/scoring-permission)?$/);
  if (m && m[1] === "sofia") {
    if (m[2] !== "/calls" && m[2] !== "/recording-link")
      return json(
        { detail: "not applicable for Retell — permissions ride the scoring pipeline" },
        404
      );
    const deny = await requirePrivileged();
    if (deny) return deny;
    if (!retellKey)
      return json(
        { detail: "RETELL_API_KEY app secret not configured — add it in the Sandy Dashboard (Edit Secrets)." },
        503
      );
    if (m[2] === "/recording-link") {
      // Fresh 24h-signed WAV URL per click (v3 list items omit recordings).
      const callId = url.searchParams.get("call_id") ?? "";
      if (!callId) return json({ detail: "call_id required" }, 422);
      const { getRetellRecordingUrl } = await import("../lib/providers/retell.js");
      try {
        return json(await getRetellRecordingUrl(retellKey, callId));
      } catch (err) {
        const status = (err as any)?.status ?? 502;
        return json({ detail: String((err as any)?.message ?? err) }, status);
      }
    }
    const config = await loadTeamConfig(db, "sofia");
    const { listRetellCalls } = await import("../lib/providers/retell.js");
    const page = await listRetellCalls(retellKey, {
      agentIds: config.provider_config?.agent_ids ?? [],
      limit: 50,
      paginationKey: url.searchParams.get("pagination_key") ?? undefined,
    });
    // D1 joins: scored evals + in-flight queue jobs, keyed by call id.
    const ids = page.items.map((i: any) => i.call_id).filter(Boolean);
    const scored = new Map<string, any>();
    if (ids.length) {
      const rows = (
        await db
          .prepare(
            `SELECT id, dialpad_call_id, state, scoring_status, overall_score
             FROM qa_evaluations WHERE team_id = 'sofia'
             AND dialpad_call_id IN (${ids.map(() => "?").join(",")})`
          )
          .bind(...ids)
          .all<any>()
      ).results;
      for (const r of rows) scored.set(r.dialpad_call_id, r);
    }
    const queued = new Map<string, string>(
      (
        await db
          .prepare(
            "SELECT call_id, status FROM qa_score_queue WHERE team_id = 'sofia' AND status IN ('queued','triggering','running')"
          )
          .all<any>()
      ).results.map((r: any) => [r.call_id, r.status])
    );
    return json({
      items: page.items.map((i: any) => ({
        ...i,
        evaluation: scored.get(i.call_id) ?? null,
        queue_status: queued.get(i.call_id) ?? null,
      })),
      pagination_key: page.pagination_key,
      has_more: page.has_more,
      filtered_app_side: page.filtered_app_side,
    });
  }
  if (m && KNOWN_TEAMS.has(m[1])) {
    const deny = await requirePrivileged();
    if (deny) return deny;
    return lookupRoutes(db, m[1], m[2] ?? "", url, request, dialpadKey);
  }

  // ── team APIs: /api/{team}/team/* ─────────────────────────────────────────
  m = path.match(/^\/api\/([^/]+)\/(team\/[a-z_]+|events\/stream)$/);
  if (!m) return null;
  const teamId = m[1];
  const sub = m[2];
  if (!KNOWN_TEAMS.has(teamId)) return json({ detail: "unknown team" }, 404);

  if (sub === "events/stream") return sseStream(db, teamId, request);

  const config = await loadTeamConfig(db, teamId);

  if (sub === "team/sections") {
    return json(
      config.sections_by_number.map((s) => ({
        id: s.id,
        history_id: s.history_id,
        name: s.name,
        section_number: s.section_number,
        score_type: s.score_type,
        audio_dependent: s.audio_dependent,
        na_applicable: s.na_applicable,
        auto_value: s.auto_value,
      }))
    );
  }

  if (sub === "team/mails") {
    const rows = await db
      .prepare(
        "SELECT name, email, supervisor_email, canonical_name FROM qa_agents WHERE team_id = ? AND active = 1 ORDER BY id"
      )
      .bind(teamId)
      .all<any>();
    return json(
      rows.results.map((r) => ({
        agent_name: r.name,
        email: r.email ?? "",
        supervisor: r.supervisor_email || null,
        canonical_name: r.canonical_name || null,
      }))
    );
  }

  if (sub === "team/stats") {
    const p = url.searchParams;
    const days = Math.min(730, Math.max(0, parseInt(p.get("days") ?? "90", 10) || 0));
    const filters = {
      days,
      active_only: p.get("active_only") !== "false",
      supervisor: p.get("supervisor") ?? "",
      date_from: p.get("date_from"),
      date_to: p.get("date_to"),
    };
    const frame = await fetchHistoryFrame(db, config);
    return json(assembleTeamStats(frame, config, filters, new Date()));
  }

  if (sub === "team/evals") {
    const p = url.searchParams;
    const ym = p.get("year_month") ?? "";
    if (!/^\d{4}-(0[1-9]|1[0-2])$/.test(ym))
      return json({ detail: "year_month must be YYYY-MM" }, 422);
    const frame = await fetchHistoryFrame(db, config);
    return json(
      assembleTeamEvals(
        frame,
        config,
        ym,
        p.get("active_only") !== "false",
        p.get("supervisor") ?? ""
      )
    );
  }

  return null;
}

// ── drill-down list endpoints ───────────────────────────────────────────────
// EvaluationRecord-shaped rows (the fields the dashboard drill-downs render:
// agent_name / timestamp / overall_score / eval_id / caller_name, plus the
// cheap extras). Built from the same canonicalized frame the stats use.

function recordFromFrameRow(r: import("../lib/historyFrame.js").FrameRow) {
  return {
    timestamp: new Date(r.ts).toISOString(),
    agent_name: r.agent,
    agent_email: r.agent_email,
    manager_email: r.manager_email,
    overall_score: r.overall_score,
    sections: {},
    eval_id: r.eval_id || null,
    // r.dialpad_link is set only on retell rows (public_log_url).
    dialpad_link:
      r.dialpad_link ||
      (r.eval_id ? `https://dialpad.com/callhistory/callreview/${r.eval_id}` : null),
    caller_name: r.caller_name || null,
    source: "ai",
  };
}

function windowFilter(
  rows: import("../lib/historyFrame.js").FrameRow[],
  url: URL,
  defaultDays: number,
  maxDays: number
) {
  const p = url.searchParams;
  const dateFrom = p.get("date_from");
  const dateTo = p.get("date_to");
  if (dateFrom && dateTo) {
    const from = Date.parse(`${dateFrom}T00:00:00Z`);
    const to = Date.parse(`${dateTo}T23:59:59.999Z`);
    return rows.filter((r) => r.ts >= from && r.ts <= to);
  }
  const days = Math.min(
    maxDays,
    Math.max(1, parseInt(p.get("days") ?? String(defaultDays), 10) || defaultDays)
  );
  const cutoff = Date.now() - days * 86_400_000;
  return rows.filter((r) => r.ts >= cutoff);
}

async function datapointsList(db: D1Database, teamId: string, url: URL) {
  const p = url.searchParams;
  const bin = p.get("bin");
  const agent = p.get("agent");
  if (!bin && !agent)
    return json({ detail: "Provide 'bin' (e.g. '81-90') or 'agent' query parameter" }, 400);
  const config = await loadTeamConfig(db, teamId);
  let rows = await fetchHistoryFrame(db, config);
  if (agent) {
    const a = agent.trim().toLowerCase();
    rows = rows.filter((r) => r.agent.toLowerCase() === a);
  }
  rows = windowFilter(rows, url, 90, 365);
  if (p.get("active_only") !== "false" && !agent)
    rows = rows.filter((r) => r.is_active);
  if (bin) {
    const parts = bin.split("-");
    const lo = Number(parts[0]);
    const hi = Number(parts[1]);
    if (!Number.isFinite(lo) || !Number.isFinite(hi))
      return json({ detail: `Invalid bin format '${bin}', expected 'lo-hi'` }, 400);
    rows = rows.filter((r) => r.overall_score >= lo && r.overall_score <= hi);
  }
  return json(rows.map(recordFromFrameRow));
}

async function agentHistory(
  db: D1Database,
  teamId: string,
  agent: string,
  url: URL
) {
  // Mirrors backend/routes/dashboard.py::agent_history — provider-shaped
  // records WITH section content (the per-agent dashboard renders section
  // trends); raw-name match, 404 on empty, range overrides days.
  const config = await loadTeamConfig(db, teamId);
  const p = url.searchParams;
  const dateFrom = p.get("date_from");
  const dateTo = p.get("date_to");
  let records: any[];
  if (dateFrom && dateTo) {
    const fromMs = Date.parse(`${dateFrom}T00:00:00Z`);
    const toMs = Date.parse(`${dateTo}T23:59:59.999Z`);
    if (Number.isNaN(fromMs) || Number.isNaN(toMs))
      return json({ detail: "date_from / date_to must be ISO YYYY-MM-DD" }, 422);
    if (toMs < fromMs)
      return json({ detail: "date_to must be on or after date_from" }, 422);
    records = await fetchRecords(db, config, { agentRaw: agent, fromMs, toMs });
    if (!records.length)
      return json(
        { detail: `No evaluations found for '${agent}' between ${dateFrom} and ${dateTo}` },
        404
      );
    return json(records);
  }
  const days = Math.min(730, Math.max(1, parseInt(p.get("days") ?? "30", 10) || 30));
  records = await fetchRecords(db, config, { agentRaw: agent, days });
  if (!records.length)
    return json(
      { detail: `No evaluations found for '${agent}' in the last ${days} days` },
      404
    );
  return json(records);
}

// ── lookup (Dialpad-backed) — port of backend/routes/lookup.py ──────────────

async function lookupRoutes(
  db: D1Database,
  teamId: string,
  sub: string,
  url: URL,
  request: Request,
  dialpadKey?: string
): Promise<Response> {
  const p = url.searchParams;

  if (sub === "/scoring-permission") {
    // Everyone who can reach /lookup is on the LOOKUP_ALLOW list = QA
    // staff → privileged-key semantics (may score anyone; the team-pick
    // modal opens when the agent is unrostered). Mirrors Railway's
    // privileged-caller branch in backend/routes/lookup.py.
    const email = p.get("email") ?? "";
    const resolved = email ? await resolveTeamForAgent(db, email) : null;
    return json({
      agent_email: email,
      resolved_team: resolved,
      can_score: true,
      needs_team_pick: resolved === null,
    });
  }

  if (!dialpadKey)
    return json(
      {
        detail:
          "DIALPAD_API_KEY app secret not configured — add it in the Sandy Dashboard (Edit Secrets) to enable /lookup.",
      },
      503
    );
  const auth = { authorization: `Bearer ${dialpadKey}` };
  const DP = "https://dialpad.com/api/v2";
  const epochIso = (v: any) =>
    v ? new Date(Number(v)).toISOString() : null;

  if (sub === "" ) {
    const email = p.get("email");
    if (!email) return json({ detail: "email query param required" }, 422);
    const r = await fetch(`${DP}/users?email=${encodeURIComponent(email)}`, {
      headers: auth,
    });
    if (!r.ok) return json({ detail: `Dialpad ${r.status}` }, 502);
    const items = ((await r.json()) as any).items ?? [];
    if (!items.length)
      return json({ detail: `No Dialpad user found for '${email}'` }, 404);
    const u = items[0];
    return json({
      id: String(u.id ?? ""),
      display_name: u.display_name ?? "",
      first_name: u.first_name ?? "",
      last_name: u.last_name ?? "",
      emails: u.emails ?? [],
      extension: u.extension ?? "",
      phone_numbers: u.phone_numbers ?? [],
      job_title: u.job_title ?? "",
      state: u.state ?? "",
      license: u.license ?? "",
      is_admin: u.is_admin ?? false,
      is_online: u.is_online ?? false,
      is_available: u.is_available ?? false,
      is_on_duty: u.is_on_duty ?? false,
      on_duty_status: u.on_duty_status ?? "",
      timezone: u.timezone ?? "",
      office_id: String(u.office_id ?? ""),
      date_added: u.date_added ?? "",
      duty_status_started: u.duty_status_started ?? "",
      groups: (u.group_details ?? []).map((g: any) => ({
        group_id: String(g.group_id ?? ""),
        group_type: g.group_type ?? "",
        role: g.role ?? "",
      })),
    });
  }

  if (sub === "/calls") {
    const userId = p.get("user_id");
    if (!userId) return json({ detail: "user_id query param required" }, 422);
    const limit = Math.min(100, Math.max(1, parseInt(p.get("limit") ?? "10", 10) || 10));
    const baseParams = new URLSearchParams({ target_id: userId, target_type: "user" });
    for (const [inKey, outKey] of [
      ["date_start", "started_after"],
      ["date_end", "started_before"],
    ] as const) {
      const v = p.get(inKey);
      if (v) {
        const t = Date.parse(v.includes("Z") || v.includes("+") ? v : v + "Z");
        if (Number.isNaN(t))
          return json({ detail: `${inKey} must be ISO format (YYYY-MM-DDTHH:MM)` }, 400);
        baseParams.set(outKey, String(t));
      }
    }
    // Direction/duration aren't Dialpad query filters (the API only takes
    // target/started_after/started_before/cursor/limit) — filtering happens
    // in-worker AFTER Dialpad's pagination (Uriel's v0.37–v0.39 addition).
    // A filtered request therefore cursor-walks Dialpad pages, accumulating
    // matches until `limit` are collected or history/cap runs out — one
    // page of 10 raw calls rarely contains matches for a compound filter
    // (the "both filters return nothing" report). Every fully-consumed
    // page's matches are returned (may exceed `limit` slightly) so the
    // returned cursor never skips matches inside a partially-used page.
    const direction = p.get("direction"); // "inbound" | "outbound" | null (any)
    const minDurationSecs = p.get("min_duration");
    const maxDurationSecs = p.get("max_duration");
    const minDurationMs = minDurationSecs ? Number(minDurationSecs) * 1000 : null;
    const maxDurationMs = maxDurationSecs ? Number(maxDurationSecs) * 1000 : null;
    const filtersActive =
      !!direction || minDurationMs !== null || maxDurationMs !== null;
    const MAX_FILTER_PAGES = 10; // × 50 raw calls — bounded Dialpad chaining
    const pageLimit = filtersActive ? 50 : limit;

    const FLAG_MS = 25 * 60 * 1000;
    const collected: any[] = [];
    let cursor = p.get("cursor") || null;
    let nextCursor: string | null = null;
    let scanned = 0;
    for (let page = 0; page < (filtersActive ? MAX_FILTER_PAGES : 1); page++) {
      const params = new URLSearchParams(baseParams);
      params.set("limit", String(pageLimit));
      if (cursor) params.set("cursor", cursor);
      const r = await fetch(`${DP}/call?${params}`, { headers: auth });
      if (!r.ok) {
        if (!collected.length && page === 0)
          return json({ user_id: userId, call_count: 0, calls: [], cursor: null });
        break; // keep what previous pages yielded; cursor resumes there
      }
      const data = (await r.json()) as any;
      const items: any[] = data.items ?? [];
      scanned += items.length;
      nextCursor = data.cursor || null;
      for (const c of items) {
        const rec = c.recording_details?.[0] ?? null;
        const duration = c.total_duration || c.duration || 0;
        if (direction && (c.direction ?? "") !== direction) continue;
        if (minDurationMs !== null && duration < minDurationMs) continue;
        if (maxDurationMs !== null && duration > maxDurationMs) continue;
        collected.push({
          call_id: String(c.call_id ?? ""),
          date_started: epochIso(c.date_started),
          date_connected: epochIso(c.date_connected),
          date_ended: epochIso(c.date_ended),
          duration,
          direction: c.direction ?? "",
          was_recorded: !!c.recording_details,
          recording_id: rec ? String(rec.id ?? "") : "",
          recording_url: rec ? rec.url ?? "" : "",
          recording_duration: rec ? Math.trunc(Number(rec.duration ?? 0)) : 0,
          recording_type: rec ? rec.recording_type ?? "" : "",
          is_transferred: c.is_transferred ?? false,
          external_number: c.external_number ?? "",
          internal_number: c.internal_number ?? "",
          contact_name: c.contact?.name ?? "",
          contact_phone: c.contact?.phone ?? "",
          mos_score: c.mos_score ?? null,
          entry_point_call_id: String(c.entry_point_call_id ?? ""),
          _flagged_long_call: duration > FLAG_MS,
        });
      }
      cursor = nextCursor;
      if (collected.length >= limit || !nextCursor) break;
    }
    return json({
      user_id: userId,
      call_count: collected.length,
      calls: collected,
      cursor: nextCursor,
      scanned,
      filters_active: filtersActive,
    });
  }

  if (sub === "/recording-link" && request.method === "POST") {
    const recordingId = p.get("recording_id");
    if (!recordingId) return json({ detail: "recording_id required" }, 422);
    const r = await fetch(`${DP}/recordingsharelink`, {
      method: "POST",
      headers: { ...auth, "content-type": "application/json" },
      body: JSON.stringify({
        privacy: "company",
        recording_type: p.get("recording_type") ?? "admincallrecording",
        recording_id: recordingId,
      }),
    });
    if (r.ok) {
      const link = ((await r.json()) as any).access_link;
      if (link) return json({ link });
    }
    return json({ detail: "Could not generate recording link" }, 404);
  }

  return json({ detail: "not found" }, 404);
}

// ── SSE: D1-tailed event stream (PortManifest §6.1) ─────────────────────────
// Bounded ~50 s stream; EventSource auto-reconnects with Last-Event-ID so no
// event is missed across stream lifetimes. Initial cursor = current max(id)
// (only NEW events push; page data comes from the APIs).

const SSE_LIFETIME_MS = 50_000;
const SSE_POLL_MS = 3_000;

async function sseStream(
  db: D1Database,
  teamId: string,
  request: Request
): Promise<Response> {
  const lastEventId =
    request.headers.get("Last-Event-ID") ??
    new URL(request.url).searchParams.get("cursor");

  const encoder = new TextEncoder();
  const stream = new ReadableStream({
    async start(controller) {
      const send = (s: string) => controller.enqueue(encoder.encode(s));
      send(": connected\n\n");
      let cursor: number;
      if (lastEventId !== null && /^\d+$/.test(lastEventId)) {
        cursor = parseInt(lastEventId, 10);
      } else {
        const row = await db
          .prepare("SELECT COALESCE(max(id), 0) AS m FROM qa_events WHERE team_id = ?")
          .bind(teamId)
          .first<{ m: number }>();
        cursor = row?.m ?? 0;
      }
      const deadline = Date.now() + SSE_LIFETIME_MS;
      try {
        while (Date.now() < deadline) {
          const events = await db
            .prepare(
              "SELECT id, type, payload FROM qa_events WHERE team_id = ? AND id > ? ORDER BY id LIMIT 50"
            )
            .bind(teamId, cursor)
            .all<{ id: number; type: string; payload: string }>();
          if (events.results.length) {
            for (const e of events.results) {
              send(`id: ${e.id}\nevent: ${e.type}\ndata: ${e.payload.replace(/\n/g, " ")}\n\n`);
              cursor = e.id;
            }
          } else {
            send(": heartbeat\n\n");
          }
          await new Promise((r) => setTimeout(r, SSE_POLL_MS));
        }
      } catch {
        // client went away or D1 hiccup — end the stream; EventSource reconnects
      }
      try {
        controller.close();
      } catch {}
    },
  });
  return new Response(stream, {
    headers: {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache",
      "X-Accel-Buffering": "no",
    },
  });
}

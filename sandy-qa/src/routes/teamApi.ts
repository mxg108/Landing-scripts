// Worker routes for the ported QA pages + team analytics APIs.
// Pages are the Railway originals served verbatim (?raw imports) — visual
// parity by construction; the APIs they consume are implemented here against
// D1 (PortManifest §4/§5). SSE is the §6.1 D1-as-bus design.

import { loadTeamConfig } from "../lib/teamConfig.js";
import { fetchHistoryFrame } from "../lib/historyFrame.js";
import { assembleTeamEvals, assembleTeamStats } from "../lib/teamStats.js";
// @ts-ignore vite ?raw
import teamDashboardHtml from "../../pages/team_dashboard.html?raw";
// @ts-ignore vite ?raw
import teamEvalsHtml from "../../pages/team_evals.html?raw";
// @ts-ignore vite ?raw
import headerCss from "../../pages/static/header.css?raw";
// @ts-ignore vite ?raw
import headerJs from "../../pages/static/header.js?raw";

const RAILWAY_BASE = "https://hellolanding-qa.up.railway.app";
const KNOWN_TEAMS = new Set(["member_support", "sales"]);

const html = (body: string) =>
  new Response(body, { headers: { "Content-Type": "text/html; charset=utf-8" } });
const json = (data: unknown, status = 200) =>
  new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json" },
  });

export async function handleTeamRoutes(
  request: Request,
  db: D1Database,
  url: URL
): Promise<Response | null> {
  const path = url.pathname;

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

  // Interim: datapoint drill-down ports in the next slice. Keep the link
  // graph honest — page exists, explains itself, links to Railway.
  m = path.match(/^\/datapoint\/([^/]+)\/([^/]+)$/);
  if (m) {
    const target = `${RAILWAY_BASE}/datapoint/${m[1]}/${encodeURIComponent(m[2])}`;
    return html(
      `<!DOCTYPE html><html><head><title>Datapoint — porting</title></head>
<body style="font-family:monospace;background:#E7EFFB;color:#15192D;display:grid;place-items:center;min-height:100vh;margin:0">
<div style="background:#fff;border:1px solid #c9d5e8;border-radius:12px;padding:32px;max-width:520px">
<h2 style="margin-top:0">Datapoint drill-down: next port slice</h2>
<p>This page hasn't moved to Sandy yet (strangler order: dashboards → stats → datapoint).</p>
<p><a href="${target}" style="color:#1A61D9">Open this datapoint on Railway &rsaquo;</a></p>
<p><a href="javascript:history.back()" style="color:#5a6478">&larr; Back</a></p>
</div></body></html>`
    );
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

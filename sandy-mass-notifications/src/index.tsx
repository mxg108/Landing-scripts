// mass-notifications — Sandy worker entry point.
// SSR React app: campaign management for property mass notifications.
// See sandy-mass-notifications/PRD.md for the full design.

import "./polyfills.js";
import { renderToString } from "react-dom/server";
import App, { type AppProps, type CampaignRow, type RecipientRow, type RoleRow } from "./App.js";
// @ts-ignore — Vite resolves ?inline to a CSS string at build time
import styles from "./styles.css?inline";
import { createDb, campaigns, recipients, roles, workflowRuns, appConfig } from "./db.js";
import { desc, eq, and, asc } from "drizzle-orm";
import { triggerWorkflowWithCallback } from "./workflow.js";
// MCP server disabled in v0.1: the agents/mcp package pulls mimetext's node
// build (`node:os`), which Sandy's deploy config rejects. Re-enable in P2 with
// a pinned agents version (qa-scoring runs one successfully).
// import { mcpHandler } from "./mcp.js";
import { serveFont } from "./design-system/fonts.js";

export interface Env {
  DB: D1Database;
  SANDY_CRON_DISPATCH_SECRET?: string;
}

// mass-notify-fetch workflow (created 2026-08-05). Override at runtime via
// app_config key 'fetch_workflow_id' — Sandy strips wrangler [vars], so the
// constant lives in source.
const FETCH_WORKFLOW_ID = "ffaa18b1-92a6-4eef-8219-c85c950c9068";
const FETCH_WORKFLOW_NAME = "mass-notify-fetch";

interface User { email: string; username: string }

function getUserFromRequest(request: Request): User {
  try {
    const jwt = request.headers.get("CF-Access-Jwt-Assertion") ?? "";
    const payload = JSON.parse(atob(jwt.split(".")[1].replace(/-/g, "+").replace(/_/g, "/")));
    const email = payload.email as string;
    return { email, username: email.split("@")[0] };
  } catch {
    return { email: "dev@local", username: "dev" };
  }
}

const ELIGIBLE_STATUSES = ["", "PENDING", "READY"];

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const db = createDb(env.DB);
    const url = new URL(request.url);

    if (request.method === "GET" && url.pathname.startsWith("/ds/fonts/")) {
      const fontResponse = serveFont(url.pathname);
      if (fontResponse) return fontResponse;
    }

    // /api/v1/mcp intentionally not mounted in v0.1 — see import note above.

    if (url.pathname === "/_sandy/cron" && request.method === "POST") {
      if (request.headers.get("X-Sandy-Cron-Secret") !== env.SANDY_CRON_DISPATCH_SECRET) {
        return Response.json({ ok: false, error: "Unauthorized" }, { status: 401 });
      }
      return Response.json({ ok: true }); // no schedules yet
    }

    // ── Workflow callback: recipients fetched from the warehouse ─────────────
    if (url.pathname === `/api/v1/callbacks/${FETCH_WORKFLOW_NAME}` && request.method === "POST") {
      if (request.headers.get("X-Sandy-Workflow-Callback") !== "verified") {
        return Response.json({ ok: false, error: "Unauthorized" }, { status: 401 });
      }
      const body = await request.json() as {
        run_id?: string; status?: string; campaign_id?: string; error?: string | null;
        recipients?: Array<Record<string, string | null>>;
        stats?: Record<string, number> | null;
      };
      if (!body.run_id) return Response.json({ ok: false, error: "missing run_id" }, { status: 400 });

      await db.update(workflowRuns)
        .set({ status: body.status ?? "error", result: JSON.stringify({ ...body, recipients: undefined }) })
        .where(eq(workflowRuns.run_id, body.run_id));

      if (body.campaign_id) {
        if (body.status === "complete") {
          // Replace previous warehouse rows; manual/CSV rows are untouched.
          await db.delete(recipients).where(and(
            eq(recipients.campaign_id, body.campaign_id),
            eq(recipients.source, "warehouse"),
          ));
          const now = new Date().toISOString();
          const rows = (body.recipients ?? []).map((r) => ({
            id: crypto.randomUUID(),
            campaign_id: body.campaign_id!,
            reservation_id: r.reservation_id ?? null,
            email: r.email ?? "",
            name: r.name ?? "",
            unit: r.unit ?? "",
            phone_e164: r.phone_e164 ?? null,
            phone_raw: r.phone_raw ?? null,
            segment_timezone: r.segment_timezone ?? null,
            market_segment: r.market_segment ?? null,
            agm_name: r.agm_name ?? null,
            source: "warehouse",
            status: r.status ?? "PENDING",
            notes: r.notes ?? "",
            email_state: "pending",
            sms_state: "off",
          }));
          // D1 caps a query at 100 bound parameters; 16 columns/row → max 6 rows.
          for (let i = 0; i < rows.length; i += 6) {
            await db.insert(recipients).values(rows.slice(i, i + 6));
          }
          await db.update(campaigns)
            .set({ status: "ready", fetch_stats_json: JSON.stringify({ ...body.stats, fetched_at: now }) })
            .where(eq(campaigns.id, body.campaign_id));
        } else {
          await db.update(campaigns)
            .set({ status: "errored", fetch_stats_json: JSON.stringify({ error: body.error ?? "unknown" }) })
            .where(eq(campaigns.id, body.campaign_id));
        }
      }
      return Response.json({ ok: true });
    }

    // ── RBAC gate (everything below is a human surface) ──────────────────────
    const user = getUserFromRequest(request);
    const roleRow = (await db.select().from(roles).where(eq(roles.email, user.email)).limit(1))[0];
    const role = roleRow?.role ?? null;

    if (url.pathname === "/api/request-access" && request.method === "POST") {
      if (!roleRow) {
        await db.insert(roles).values({
          email: user.email, role: "requested", granted_by: null,
          created_at: new Date().toISOString(),
        });
      }
      return Response.redirect(new URL("/", request.url).toString(), 303);
    }

    const isOperator = role === "admin" || role === "operator";
    if (!isOperator) {
      return renderPage({
        page: "access", user, role,
        campaigns: [], recipients: [], roleRequests: [],
      });
    }

    // ── Admin: grant/deny roles ──────────────────────────────────────────────
    if (url.pathname === "/api/roles" && request.method === "POST" && role === "admin") {
      const form = await request.formData();
      const email = form.get("email")?.toString().trim().toLowerCase();
      const newRole = form.get("role")?.toString();
      if (email && newRole && ["admin", "operator", "denied"].includes(newRole)) {
        if (newRole === "denied") {
          await db.delete(roles).where(eq(roles.email, email));
        } else {
          await db.delete(roles).where(eq(roles.email, email));
          await db.insert(roles).values({
            email, role: newRole, granted_by: user.email,
            created_at: new Date().toISOString(),
          });
        }
      }
      return Response.redirect(new URL("/", request.url).toString(), 303);
    }

    // ── Create campaign ──────────────────────────────────────────────────────
    if (url.pathname === "/api/campaigns" && request.method === "POST") {
      const form = await request.formData();
      const propertyName = form.get("property_name")?.toString().trim();
      const eventName = form.get("event_name")?.toString().trim() ?? "";
      if (!propertyName) {
        return Response.redirect(new URL("/?error=Property+name+is+required", request.url).toString(), 303);
      }
      const id = crypto.randomUUID();
      await db.insert(campaigns).values({
        id, mode: "INDIVIDUAL", property_name: propertyName, event_name: eventName,
        status: "draft", config_json: "{}", created_by: user.email,
        created_at: new Date().toISOString(),
      });
      return Response.redirect(new URL(`/c/${id}`, request.url).toString(), 303);
    }

    // ── Trigger warehouse fetch ──────────────────────────────────────────────
    const fetchMatch = url.pathname.match(/^\/api\/campaigns\/([0-9a-f-]{36})\/fetch$/);
    if (fetchMatch && request.method === "POST") {
      const cid = fetchMatch[1];
      const campaign = (await db.select().from(campaigns).where(eq(campaigns.id, cid)).limit(1))[0];
      if (!campaign) return Response.redirect(new URL("/?error=Campaign+not+found", request.url).toString(), 303);
      const wfId = (await db.select().from(appConfig).where(eq(appConfig.key, "fetch_workflow_id")).limit(1))[0]?.value
        ?? FETCH_WORKFLOW_ID;
      try {
        const run = await triggerWorkflowWithCallback(wfId, FETCH_WORKFLOW_NAME, request, {
          property_name: campaign.property_name,
          campaign_id: cid,
        });
        await db.insert(workflowRuns).values({
          run_id: run.id, workflow_name: FETCH_WORKFLOW_NAME,
          status: "pending", created_at: new Date().toISOString(),
        });
        await db.update(campaigns).set({ status: "fetching" }).where(eq(campaigns.id, cid));
        return Response.redirect(new URL(`/c/${cid}?flash=Fetch+queued`, request.url).toString(), 303);
      } catch (e: any) {
        return Response.redirect(
          new URL(`/c/${cid}?error=${encodeURIComponent(e?.message ?? String(e))}`, request.url).toString(), 303);
      }
    }

    // ── Manual recipient add ─────────────────────────────────────────────────
    const recAddMatch = url.pathname.match(/^\/api\/campaigns\/([0-9a-f-]{36})\/recipients$/);
    if (recAddMatch && request.method === "POST") {
      const cid = recAddMatch[1];
      const form = await request.formData();
      const email = form.get("email")?.toString().trim() ?? "";
      if (!/^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/.test(email)) {
        return Response.redirect(new URL(`/c/${cid}?error=Invalid+email`, request.url).toString(), 303);
      }
      await db.insert(recipients).values({
        id: crypto.randomUUID(), campaign_id: cid, email,
        name: form.get("name")?.toString().trim() ?? "",
        unit: form.get("unit")?.toString().trim() ?? "",
        source: "manual", status: "PENDING",
        email_state: "pending", sms_state: "off",
      });
      return Response.redirect(new URL(`/c/${cid}`, request.url).toString(), 303);
    }

    // ── Recipient status update / delete (field-level, never grid overwrite) ─
    const recEditMatch = url.pathname.match(/^\/api\/recipients\/([0-9a-f-]{36})$/);
    if (recEditMatch && request.method === "POST") {
      const rid = recEditMatch[1];
      const form = await request.formData();
      const action = form.get("action")?.toString();
      const row = (await db.select().from(recipients).where(eq(recipients.id, rid)).limit(1))[0];
      if (row) {
        if (action === "delete") {
          await db.delete(recipients).where(eq(recipients.id, rid));
        } else if (action === "status") {
          const status = form.get("status")?.toString() ?? "";
          if (["PENDING", "READY", "REVIEW"].includes(status)) {
            await db.update(recipients).set({ status }).where(eq(recipients.id, rid));
          }
        }
        return Response.redirect(new URL(`/c/${row.campaign_id}`, request.url).toString(), 303);
      }
      return Response.redirect(new URL("/", request.url).toString(), 303);
    }

    // ── Campaign detail page ─────────────────────────────────────────────────
    const detailMatch = url.pathname.match(/^\/c\/([0-9a-f-]{36})$/);
    if (detailMatch && request.method === "GET") {
      const cid = detailMatch[1];
      const campaign = (await db.select().from(campaigns).where(eq(campaigns.id, cid)).limit(1))[0];
      if (!campaign) return Response.redirect(new URL("/?error=Campaign+not+found", request.url).toString(), 303);
      const recRows = await db.select().from(recipients)
        .where(eq(recipients.campaign_id, cid))
        .orderBy(asc(recipients.unit), asc(recipients.email));
      return renderPage({
        page: "campaign", user, role,
        campaigns: [campaign as CampaignRow], recipients: recRows as RecipientRow[], roleRequests: [],
        flash: url.searchParams.get("flash") ?? undefined,
        error: url.searchParams.get("error") ?? undefined,
      });
    }

    // ── Home: campaign list ──────────────────────────────────────────────────
    const campaignRows = await db.select().from(campaigns).orderBy(desc(campaigns.created_at)).limit(50);
    const roleRequests = role === "admin"
      ? await db.select().from(roles).orderBy(desc(roles.created_at)).limit(50)
      : [];
    return renderPage({
      page: "home", user, role,
      campaigns: campaignRows as CampaignRow[], recipients: [],
      roleRequests: roleRequests as RoleRow[],
      flash: url.searchParams.get("flash") ?? undefined,
      error: url.searchParams.get("error") ?? undefined,
    });
  },
};

function renderPage(props: AppProps): Response {
  const appHtml = renderToString(<App {...props} />);
  const html = `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Mass Notifications</title>
  <style>${styles}</style>
</head>
<body>
  <div id="root">${appHtml}</div>
</body>
</html>`;
  return new Response(html, { headers: { "Content-Type": "text/html; charset=utf-8" } });
}

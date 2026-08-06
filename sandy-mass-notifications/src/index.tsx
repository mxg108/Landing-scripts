// mass-notifications — Sandy worker entry point.
// SSR React app: campaign management for property mass notifications.
// See sandy-mass-notifications/PRD.md for the full design.

import "./polyfills.js";
import { renderToString } from "react-dom/server";
import App, { type AppProps, type CampaignRow, type RecipientRow, type RoleRow, type RunRow } from "./App.js";
// @ts-ignore — Vite resolves ?inline to a CSS string at build time
import styles from "./styles.css?inline";
import { createDb, campaigns, recipients, roles, runs, workflowRuns, appConfig, templates } from "./db.js";
import { desc, eq, and, asc } from "drizzle-orm";
import { triggerWorkflowWithCallback } from "./workflow.js";
// MCP server disabled in v0.1: the agents/mcp package pulls mimetext's node
// build (`node:os`), which Sandy's deploy config rejects. Re-enable in P2 with
// a pinned agents version (qa-scoring runs one successfully).
// import { mcpHandler } from "./mcp.js";
import { serveFont } from "./design-system/fonts.js";
import {
  parseConfig, composeBodyTemplate, buildGlobalTokens, buildRecipientTokens,
  renderTokens, renderCard, applyTemplate, validateForDispatch, CONFIG_DEFAULTS,
  SEED_CARDS, SEED_DISCLAIMERS, formatToday,
  type CampaignConfig, type CardDef,
} from "./emailkit.js";
import type { AssetRow } from "./App.js";

export interface Env {
  DB: D1Database;
  SANDY_CRON_DISPATCH_SECRET?: string;
}

// Workflow IDs (created 2026-08-05). Overridable at runtime via app_config keys
// 'fetch_workflow_id' / 'dispatch_workflow_id' — Sandy strips wrangler [vars],
// so the constants live in source.
const FETCH_WORKFLOW_ID = "ffaa18b1-92a6-4eef-8219-c85c950c9068";
const FETCH_WORKFLOW_NAME = "mass-notify-fetch";
const DISPATCH_WORKFLOW_ID = "1afe1706-2986-4ee9-9436-133820030f7b";
const DISPATCH_WORKFLOW_NAME = "mass-notify-dispatch";

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

const ELIGIBLE = ["", "PENDING", "READY"];

function parseIdList(s: string): string[] {
  return String(s || "").split(/[\s,]+/).map((x) => x.trim()).filter(Boolean)
    .filter((v, i, a) => a.indexOf(v) === i);
}

// Cards + disclaimers live in D1 (templates: kind='card'|'disclaimer'), seeded
// once from emailkit SEED_* and curated at /edit thereafter.
async function loadAssets(db: ReturnType<typeof createDb>) {
  let rows = await db.select().from(templates);
  if (!rows.some((r) => r.kind === "card")) {
    const now = new Date().toISOString();
    for (const c of SEED_CARDS) {
      await db.insert(templates).values({
        id: crypto.randomUUID(), name: c.key, kind: "card",
        config_json: JSON.stringify(c), active: 1, updated_by: "seed", updated_at: now,
      });
    }
    for (const d of SEED_DISCLAIMERS) {
      await db.insert(templates).values({
        id: crypto.randomUUID(), name: d.name, kind: "disclaimer",
        config_json: JSON.stringify(d), active: 1, updated_by: "seed", updated_at: now,
      });
    }
    rows = await db.select().from(templates);
  }
  const parse = (r: typeof rows[number]): AssetRow => {
    let cfg: Record<string, string> = {};
    try { cfg = JSON.parse(r.config_json); } catch { /* ignore */ }
    return { id: r.id, kind: r.kind, name: r.name, active: r.active === 1, config: cfg,
      updated_by: r.updated_by, updated_at: r.updated_at };
  };
  const cards = rows.filter((r) => r.kind === "card").map(parse);
  const disclaimers = rows.filter((r) => r.kind === "disclaimer").map(parse);
  const cardMap: Record<string, CardDef> = {};
  for (const c of cards) if (c.active) cardMap[String(c.config.key)] = c.config as unknown as CardDef;
  return { cards, disclaimers, cardMap };
}

const SAMPLE_TOKENS: Record<string, string> = {
  property_name: "Woodhill", event_name: "Sample Event",
  date_range: "Mon, Aug 10–Fri, Aug 14, 2026", today: "",
  manager_name: "Alex Doe", manager_email: "alex.doe@hellolanding.com",
  first_name: "Jordan", member_name: "Jordan Sample",
  member_email: "jordan@example.com", unit: "101",
};

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

    // ── Workflow callback: dispatch results (send / dryrun / test) ───────────
    if (url.pathname === `/api/v1/callbacks/${DISPATCH_WORKFLOW_NAME}` && request.method === "POST") {
      if (request.headers.get("X-Sandy-Workflow-Callback") !== "verified") {
        return Response.json({ ok: false, error: "Unauthorized" }, { status: 401 });
      }
      const body = await request.json() as {
        run_id?: string; status?: string; campaign_id?: string; app_run_id?: string;
        kind?: string; error?: string | null; quota_remaining?: number | null;
        results?: Array<{ id: string; email: string; ok: boolean; error: string | null }>;
      };
      if (!body.run_id) return Response.json({ ok: false, error: "missing run_id" }, { status: 400 });

      await db.update(workflowRuns)
        .set({ status: body.status ?? "error", result: JSON.stringify({ ...body, results: undefined }) })
        .where(eq(workflowRuns.run_id, body.run_id));

      const now = new Date().toISOString();
      const results = body.results ?? [];
      const okCount = results.filter((r) => r.ok).length;

      // Per-recipient state updates (test uses a synthetic recipient — no rows match).
      for (const r of results) {
        if (!r.id || r.id === "test") continue;
        if (r.ok) {
          if (body.kind === "send") {
            await db.update(recipients)
              .set({ status: "SENT", email_state: "sent", email_sent_at: now })
              .where(eq(recipients.id, r.id));
          } else if (body.kind === "dryrun") {
            await db.update(recipients)
              .set({ status: "DRAFT" })
              .where(eq(recipients.id, r.id));
          }
        } else {
          await db.update(recipients)
            .set({ email_state: "error", notes: r.error ?? "send failed" })
            .where(eq(recipients.id, r.id));
        }
      }

      if (body.app_run_id) {
        await db.update(runs)
          .set({
            count: okCount,
            completed_at: now,
            error: body.status === "error" ? (body.error ?? "unknown")
              : results.some((r) => !r.ok) ? `${results.filter((r) => !r.ok).length} failed` : null,
          })
          .where(eq(runs.id, body.app_run_id));
      }

      if (body.campaign_id) {
        const newStatus = body.status !== "complete" ? "errored"
          : body.kind === "send" ? "complete" : "ready";
        await db.update(campaigns)
          .set(newStatus === "complete" ? { status: newStatus, completed_at: now } : { status: newStatus })
          .where(eq(campaigns.id, body.campaign_id));
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
        campaigns: [], recipients: [], roleRequests: [], runs: [],
      });
    }

    // ── Admin: grant/deny roles ──────────────────────────────────────────────
    if (url.pathname === "/api/roles" && request.method === "POST" && role === "admin") {
      const form = await request.formData();
      const email = form.get("email")?.toString().trim().toLowerCase();
      const newRole = form.get("role")?.toString();
      if (email && newRole && ["admin", "operator", "denied"].includes(newRole)) {
        await db.delete(roles).where(eq(roles.email, email));
        if (newRole !== "denied") {
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

    const campaignRoute = url.pathname.match(/^\/(?:api\/campaigns|c)\/([0-9a-f-]{36})(\/[a-z-]+)?$/);
    const cid = campaignRoute?.[1];
    const action = campaignRoute?.[2];
    const campaign = cid
      ? (await db.select().from(campaigns).where(eq(campaigns.id, cid)).limit(1))[0]
      : undefined;
    if (cid && !campaign) {
      return Response.redirect(new URL("/?error=Campaign+not+found", request.url).toString(), 303);
    }
    const back = (msg: string, isError = false) =>
      Response.redirect(new URL(`/c/${cid}?${isError ? "error" : "flash"}=${encodeURIComponent(msg)}`, request.url).toString(), 303);

    // ── Trigger warehouse fetch ──────────────────────────────────────────────
    if (campaign && action === "/fetch" && request.method === "POST") {
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
        await db.update(campaigns).set({ status: "fetching" }).where(eq(campaigns.id, cid!));
        return back("Fetch queued");
      } catch (e: any) {
        return back(e?.message ?? String(e), true);
      }
    }

    // ── Save campaign config ─────────────────────────────────────────────────
    if (campaign && action === "/config" && request.method === "POST") {
      const form = await request.formData();
      const cfg = parseConfig(campaign.config_json);
      for (const key of Object.keys(CONFIG_DEFAULTS) as (keyof CampaignConfig)[]) {
        if (typeof CONFIG_DEFAULTS[key] === "boolean") {
          (cfg as any)[key] = form.get(key) === "on";
        } else if (form.has(key)) {
          const v = form.get(key)!.toString();
          (cfg as any)[key] = typeof CONFIG_DEFAULTS[key] === "number" ? (Number(v) || CONFIG_DEFAULTS[key]) : v;
        }
      }
      const eventName = form.get("event_name")?.toString().trim() ?? campaign.event_name;
      await db.update(campaigns)
        .set({ config_json: JSON.stringify(cfg), event_name: eventName })
        .where(eq(campaigns.id, cid!));
      return back("Configuration saved");
    }

    // ── Apply template ───────────────────────────────────────────────────────
    if (campaign && action === "/template" && request.method === "POST") {
      const form = await request.formData();
      const name = form.get("name")?.toString() ?? "";
      const applied = applyTemplate(parseConfig(campaign.config_json), name);
      if (!applied) return back(`Unknown template: ${name}`, true);
      await db.update(campaigns)
        .set({
          config_json: JSON.stringify(applied.cfg),
          event_name: applied.event_name ?? campaign.event_name,
        })
        .where(eq(campaigns.id, cid!));
      return back(`Template "${name}" applied — fill in manager, dates, and review before sending`);
    }

    // ── Instant-apply chiclets: notification card / disclaimer ───────────────
    if (campaign && action === "/config-card" && request.method === "POST") {
      const form = await request.formData();
      const key = form.get("card")?.toString() ?? "";
      const { cardMap } = await loadAssets(db);
      if (key && !cardMap[key]) return back(`Unknown card: ${key}`, true);
      const cfg = parseConfig(campaign.config_json);
      cfg.notification_card = key;
      await db.update(campaigns).set({ config_json: JSON.stringify(cfg) }).where(eq(campaigns.id, cid!));
      return back(key ? `Card set: ${key}` : "Card removed");
    }
    if (campaign && action === "/config-disclaimer" && request.method === "POST") {
      const form = await request.formData();
      const tid = form.get("template_id")?.toString() ?? "";
      const { disclaimers } = await loadAssets(db);
      const d = disclaimers.find((x) => x.id === tid && x.active);
      if (!d) return back("Unknown disclaimer", true);
      const cfg = parseConfig(campaign.config_json);
      cfg.disclaimer_html = String(d.config.html ?? "");
      cfg.include_disclaimer = true;
      await db.update(campaigns).set({ config_json: JSON.stringify(cfg) }).where(eq(campaigns.id, cid!));
      return back(`Disclaimer set: ${d.name}`);
    }

    // ── SSE: live campaign status (replaces refresh-to-see-results) ──────────
    if (campaign && action === "/events" && request.method === "GET") {
      const enc = new TextEncoder();
      const stream = new ReadableStream({
        async start(controller) {
          const send = (obj: unknown) =>
            controller.enqueue(enc.encode(`data: ${JSON.stringify(obj)}\n\n`));
          try {
            for (let i = 0; i < 25; i++) { // ~50s, then EventSource auto-reconnects
              const row = (await db.select({ status: campaigns.status })
                .from(campaigns).where(eq(campaigns.id, cid!)).limit(1))[0];
              send({ status: row?.status ?? "gone" });
              if (!row || !["fetching", "sending"].includes(row.status)) break;
              await new Promise((r) => setTimeout(r, 2000));
            }
          } catch { /* client went away */ }
          try { controller.close(); } catch { /* already closed */ }
        },
      });
      return new Response(stream, {
        headers: {
          "Content-Type": "text/event-stream",
          "Cache-Control": "no-cache",
          "Connection": "keep-alive",
        },
      });
    }

    // ── Dispatch: send / dryrun / test ───────────────────────────────────────
    if (campaign && action === "/dispatch" && request.method === "POST") {
      const form = await request.formData();
      const kind = form.get("kind")?.toString() ?? "";
      if (!["send", "dryrun", "test"].includes(kind)) return back("Bad dispatch kind", true);
      if (kind === "send" && form.get("confirm") !== "on") {
        return back("Check the confirmation box to send", true);
      }
      const cfg = parseConfig(campaign.config_json);
      const { cardMap } = await loadAssets(db);
      const allRows = await db.select().from(recipients)
        .where(eq(recipients.campaign_id, cid!))
        .orderBy(asc(recipients.unit), asc(recipients.email));
      const eligibleRows = allRows.filter((r) => ELIGIBLE.includes(r.status));

      const errors = validateForDispatch(cfg, campaign.property_name, eligibleRows.length, kind,
        new Set(Object.keys(cardMap)));
      if (errors.length) return back(errors.join(" "), true);

      const limit = kind === "dryrun" ? cfg.dry_run_limit : cfg.max_per_run;
      const targets = eligibleRows.slice(0, limit);
      const globals = buildGlobalTokens(campaign.property_name, campaign.event_name, cfg);
      const configAttachmentIds = parseIdList(cfg.attachment_file_ids);

      const wfRecipients = kind === "test"
        ? [{
            id: "test", email: user.email,
            name: targets[0]?.name || "Resident Test",
            unit: targets[0]?.unit || "101",
            attachmentIds: [],
          }]
        : targets.map((r) => ({
            id: r.id, email: r.email, name: r.name, unit: r.unit,
            attachmentIds: parseIdList(r.attach_ids_json ?? ""),
          }));

      const appRunId = crypto.randomUUID();
      const rowStates = kind === "send"
        ? targets.map((r) => ({ id: r.id, prevStatus: r.status }))
        : null;

      const wfId = (await db.select().from(appConfig).where(eq(appConfig.key, "dispatch_workflow_id")).limit(1))[0]?.value
        ?? DISPATCH_WORKFLOW_ID;
      try {
        const run = await triggerWorkflowWithCallback(wfId, DISPATCH_WORKFLOW_NAME, request, {
          campaign_id: cid,
          app_run_id: appRunId,
          kind,
          config: {
            subjectTemplate: cfg.subject_template,
            bodyTemplate: composeBodyTemplate(cfg, cardMap),
            senderName: cfg.sender_display_name,
            replyTo: cfg.reply_to,
            cc: [cfg.manager_email, cfg.cc_extra].filter(Boolean).join(","),
            includeUnitLine: cfg.include_unit_line,
            globalTokens: globals,
            configAttachmentIds,
          },
          recipients: wfRecipients,
        });
        await db.insert(runs).values({
          id: appRunId, campaign_id: cid!, kind, actor: user.email,
          count: 0, row_states_json: rowStates ? JSON.stringify(rowStates) : null,
          started_at: new Date().toISOString(),
        });
        await db.insert(workflowRuns).values({
          run_id: run.id, workflow_name: DISPATCH_WORKFLOW_NAME,
          status: "pending", created_at: new Date().toISOString(),
        });
        if (kind === "send") {
          await db.update(campaigns).set({ status: "sending" }).where(eq(campaigns.id, cid!));
        }
        const label = kind === "send" ? `Sending to ${wfRecipients.length} recipients`
          : kind === "dryrun" ? `Creating ${wfRecipients.length} drafts in member.support@`
          : `Test email queued to ${user.email}`;
        return back(`${label} — results will appear here automatically`);
      } catch (e: any) {
        return back(e?.message ?? String(e), true);
      }
    }

    // ── Undo the latest completed send ───────────────────────────────────────
    if (campaign && action === "/undo" && request.method === "POST") {
      const lastSend = (await db.select().from(runs)
        .where(and(eq(runs.campaign_id, cid!), eq(runs.kind, "send")))
        .orderBy(desc(runs.started_at)).limit(1))[0];
      if (!lastSend || !lastSend.completed_at || !lastSend.row_states_json) {
        return back("No completed send run to undo", true);
      }
      let states: Array<{ id: string; prevStatus: string }> = [];
      try { states = JSON.parse(lastSend.row_states_json); } catch { /* noop */ }
      for (const s of states) {
        await db.update(recipients)
          .set({ status: s.prevStatus ?? "", email_state: "pending", email_sent_at: null })
          .where(eq(recipients.id, s.id));
      }
      const now = new Date().toISOString();
      await db.insert(runs).values({
        id: crypto.randomUUID(), campaign_id: cid!, kind: "undo", actor: user.email,
        count: states.length, row_states_json: null,
        started_at: now, completed_at: now,
      });
      await db.update(campaigns).set({ status: "ready", completed_at: null }).where(eq(campaigns.id, cid!));
      return back(`Undo complete — ${states.length} recipients reverted (emails already sent are NOT recalled)`);
    }

    // ── Manual recipient add ─────────────────────────────────────────────────
    if (campaign && action === "/recipients" && request.method === "POST") {
      const form = await request.formData();
      const email = form.get("email")?.toString().trim() ?? "";
      if (!/^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/.test(email)) return back("Invalid email", true);
      await db.insert(recipients).values({
        id: crypto.randomUUID(), campaign_id: cid!, email,
        name: form.get("name")?.toString().trim() ?? "",
        unit: form.get("unit")?.toString().trim() ?? "",
        source: "manual", status: "PENDING",
        email_state: "pending", sms_state: "off",
      });
      return back("Recipient added");
    }

    // ── /edit — cards & disclaimers management ───────────────────────────────
    if (url.pathname === "/edit" && request.method === "GET") {
      const { cards, disclaimers } = await loadAssets(db);
      return renderPage({
        page: "edit", user, role,
        campaigns: [], recipients: [], roleRequests: [], runs: [],
        cards, disclaimers,
        flash: url.searchParams.get("flash") ?? undefined,
        error: url.searchParams.get("error") ?? undefined,
      });
    }
    const editorMatch = url.pathname.match(/^\/edit\/(card|disclaimer)\/(new|[0-9a-f-]{36})$/);
    if (editorMatch && request.method === "GET") {
      const [, kind, idOrNew] = editorMatch;
      const { cards, disclaimers } = await loadAssets(db);
      const pool = kind === "card" ? cards : disclaimers;
      const editing = idOrNew === "new" ? undefined : pool.find((a) => a.id === idOrNew);
      if (idOrNew !== "new" && !editing) {
        return Response.redirect(new URL("/edit?error=Not+found", request.url).toString(), 303);
      }
      // Editor preview: card rendered in its shell / disclaimer as-is, sample tokens.
      const sample = { ...SAMPLE_TOKENS, today: formatToday("America/Mexico_City") };
      let editorPreviewHtml = "";
      if (editing) {
        editorPreviewHtml = kind === "card"
          ? renderTokens(renderCard(editing.config as unknown as CardDef), sample, true)
          : renderTokens(String(editing.config.html ?? ""), sample, true);
      }
      return renderPage({
        page: "editor", user, role,
        campaigns: [], recipients: [], roleRequests: [], runs: [],
        cards, disclaimers,
        editorKind: kind as "card" | "disclaimer",
        editing, editorPreviewHtml,
        flash: url.searchParams.get("flash") ?? undefined,
        error: url.searchParams.get("error") ?? undefined,
      });
    }
    const editApiMatch = url.pathname.match(/^\/api\/edit\/(card|disclaimer)$/);
    if (editApiMatch && request.method === "POST") {
      const kind = editApiMatch[1];
      const form = await request.formData();
      const id = form.get("id")?.toString() || crypto.randomUUID();
      const isNew = !form.get("id");
      const active = form.get("active") === "on" ? 1 : 0;
      const now = new Date().toISOString();
      let name = "";
      let config: Record<string, string> = {};
      if (kind === "card") {
        const key = (form.get("key")?.toString() ?? "").trim().toUpperCase().replace(/[^A-Z0-9_]/g, "_");
        if (!key) return Response.redirect(new URL("/edit?error=Card+key+is+required", request.url).toString(), 303);
        const { cards } = await loadAssets(db);
        if (cards.some((c) => String(c.config.key) === key && c.id !== id)) {
          return Response.redirect(new URL(`/edit?error=Card+key+${key}+already+exists`, request.url).toString(), 303);
        }
        name = key;
        config = {
          key,
          label: form.get("label")?.toString().trim() || key,
          accent: form.get("accent")?.toString().trim() || "#1A61D9",
          icon: form.get("icon")?.toString().trim() || "",
          title: form.get("title")?.toString().trim() || key,
          body_html: form.get("body_html")?.toString() ?? "",
        };
      } else {
        name = form.get("name")?.toString().trim() || "Untitled disclaimer";
        config = { name, html: form.get("html")?.toString() ?? "" };
      }
      if (isNew) {
        await db.insert(templates).values({
          id, name, kind, config_json: JSON.stringify(config), active,
          updated_by: user.email, updated_at: now,
        });
      } else {
        await db.update(templates)
          .set({ name, config_json: JSON.stringify(config), active, updated_by: user.email, updated_at: now })
          .where(eq(templates.id, id));
      }
      return Response.redirect(
        new URL(`/edit/${kind}/${id}?flash=Saved`, request.url).toString(), 303);
    }

    // ── Recipient status update / delete (field-level) ───────────────────────
    const recEditMatch = url.pathname.match(/^\/api\/recipients\/([0-9a-f-]{36})$/);
    if (recEditMatch && request.method === "POST") {
      const rid = recEditMatch[1];
      const form = await request.formData();
      const act = form.get("action")?.toString();
      const row = (await db.select().from(recipients).where(eq(recipients.id, rid)).limit(1))[0];
      if (row) {
        if (act === "delete") {
          await db.delete(recipients).where(eq(recipients.id, rid));
        } else if (act === "status") {
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
    if (campaign && !action && request.method === "GET") {
      const recRows = await db.select().from(recipients)
        .where(eq(recipients.campaign_id, cid!))
        .orderBy(asc(recipients.unit), asc(recipients.email));
      const runRows = await db.select().from(runs)
        .where(eq(runs.campaign_id, cid!))
        .orderBy(desc(runs.started_at)).limit(10);
      const { cards, disclaimers, cardMap } = await loadAssets(db);

      // Server-rendered preview against a chosen (or first eligible) recipient.
      const cfg = parseConfig(campaign.config_json);
      const previewId = url.searchParams.get("preview");
      const eligibleRows = recRows.filter((r) => ELIGIBLE.includes(r.status));
      const pr = (previewId && recRows.find((r) => r.id === previewId)) || eligibleRows[0];
      const globals = buildGlobalTokens(campaign.property_name, campaign.event_name, cfg);
      const tokens = buildRecipientTokens(
        globals,
        pr ? { email: pr.email, name: pr.name, unit: pr.unit }
           : { email: "", name: "Resident", unit: "101" },
        cfg.include_unit_line
      );
      const previewHtml = renderTokens(composeBodyTemplate(cfg, cardMap), tokens, true);
      const previewSubject = renderTokens(cfg.subject_template, tokens, false);

      // Live status watcher: SSE while fetching/sending, reload on transition.
      const watchScript = ["fetching", "sending"].includes(campaign.status) ? `
(function(){
  if (!window.EventSource) return;
  var initial = ${JSON.stringify(campaign.status)};
  function watch(){
    var es = new EventSource(location.pathname.replace(/\\/$/, "") + "/events");
    es.onmessage = function(ev){
      try {
        var d = JSON.parse(ev.data);
        if (d.status && d.status !== initial) { es.close(); location.reload(); }
      } catch(e){}
    };
    es.onerror = function(){ es.close(); setTimeout(watch, 3000); };
  }
  watch();
})();` : undefined;

      return renderPage({
        page: "campaign", user, role,
        campaigns: [campaign as CampaignRow], recipients: recRows as RecipientRow[],
        roleRequests: [], runs: runRows as RunRow[],
        cards, disclaimers,
        config: cfg,
        previewHtml, previewSubject,
        previewFor: pr ? pr.email : "generic (no recipients)",
        previewRecipientId: pr?.id,
        flash: url.searchParams.get("flash") ?? undefined,
        error: url.searchParams.get("error") ?? undefined,
      }, watchScript);
    }

    // ── Home: campaign list ──────────────────────────────────────────────────
    const campaignRows = await db.select().from(campaigns).orderBy(desc(campaigns.created_at)).limit(50);
    const roleRequests = role === "admin"
      ? await db.select().from(roles).orderBy(desc(roles.created_at)).limit(50)
      : [];
    return renderPage({
      page: "home", user, role,
      campaigns: campaignRows as CampaignRow[], recipients: [], runs: [],
      roleRequests: roleRequests as RoleRow[],
      flash: url.searchParams.get("flash") ?? undefined,
      error: url.searchParams.get("error") ?? undefined,
    });
  },
};

function renderPage(props: AppProps, extraScript?: string): Response {
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
  <div id="root">${appHtml}</div>${extraScript ? `\n  <script>${extraScript}</script>` : ""}
</body>
</html>`;
  return new Response(html, { headers: { "Content-Type": "text/html; charset=utf-8" } });
}

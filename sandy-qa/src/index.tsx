// Cloudflare Worker entry point — fetches data from D1 and renders the React app server-side.
// All data fetching happens here in the worker; no client-side JS bundle is needed.

// Must be first: defines the MessageChannel global that react-dom/server needs.
import "./polyfills.js";
import { renderToString } from "react-dom/server";
import App from "./App.js";
// @ts-ignore — Vite resolves ?inline to a CSS string at build time
import styles from "./styles.css?inline";
import { createDb, items, workflowRuns } from "./db.js";
import { desc, eq } from "drizzle-orm";
import { listTriggerableWorkflows, triggerWorkflow, triggerWorkflowWithCallback, type WorkflowInfo } from "./workflow.js";
import { serveFont } from "./design-system/fonts.js";
import { handleTeamRoutes } from "./routes/teamApi.js";

export interface Env {
  DB: D1Database;
  // Dashboard-set app secret (Sandy UI → Edit Secrets) — enables /lookup's
  // live Dialpad calls. Absent → lookup APIs return 503 with instructions.
  DIALPAD_API_KEY?: string;
  // Dashboard-set app secret: comma-separated emails allowed on /lookup
  // (interim deny-by-default compliance lock until per-team RBAC lands).
  LOOKUP_ALLOW?: string;
  // Dashboard-set app secrets: Pulpo MCP (SOP retrieval at prompt-build —
  // v1-essential; absent → prompts take the sop_context_missing path).
  PULPO_MCP_URL?: string;
  PULPO_MCP_TOKEN?: string;
  // Optional: pre-select a specific workflow. Leave unset to list all available.
  WORKFLOW_ID?: string;
  // Provisioned automatically by Sandy at publish time for apps with cron schedules.
  // Used to verify that /_sandy/cron requests originate from the Sandy platform.
  SANDY_CRON_DISPATCH_SECRET?: string;
}


export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const db = createDb(env.DB);
    const url = new URL(request.url);

    // GET /ds/fonts/*.woff2 — Landing design system web fonts, bundled into the
    // worker and served with immutable caching. Referenced by @font-face rules in
    // colors_and_type.css. Browsers fetch each font once and cache it.
    if (request.method === "GET" && url.pathname.startsWith("/ds/fonts/")) {
      const fontResponse = serveFont(url.pathname);
      if (fontResponse) return fontResponse;
    }

    // ── Ported QA pages + team analytics APIs (PortManifest §3/§4/§6.1) ──
    // Global guard: a thrown handler must surface as JSON, never as
    // Cloudflare's HTML 1101 page (observed: the editor's re-approve
    // showed "Unexpected token '<'" instead of the real error).
    try {
      const teamRes = await handleTeamRoutes(
        request, env.DB, url, env.DIALPAD_API_KEY, env.LOOKUP_ALLOW,
        { url: env.PULPO_MCP_URL, token: env.PULPO_MCP_TOKEN }
      );
      if (teamRes) return teamRes;
    } catch (err) {
      console.log(`[route-error] ${url.pathname}: ${String(err).slice(0, 500)}`);
      return Response.json(
        { detail: `internal: ${String((err as any)?.message ?? err).slice(0, 300)}` },
        { status: 500 }
      );
    }

    // ── SSE transport spike (PortManifest §"SSE on Sandy") ───────────────
    // Answers one question empirically: does the Sandy edge stack
    // (dispatch worker + CF Access) pass a text/event-stream through
    // UNBUFFERED? Emits one tick every 2s for 40s. Open /sse-test in a
    // browser — the page renders a green STREAMING / red BUFFERED verdict.
    // Remove both routes once the port's real /events/stream lands.
    if (url.pathname === "/events/stream" && request.method === "GET") {
      const encoder = new TextEncoder();
      const stream = new ReadableStream({
        async start(controller) {
          controller.enqueue(encoder.encode(": connected\n\n"));
          for (let i = 1; i <= 20; i++) {
            await new Promise((r) => setTimeout(r, 2000));
            controller.enqueue(
              encoder.encode(
                `id: ${i}\nevent: tick\ndata: {"n":${i},"server_t":"${new Date().toISOString()}"}\n\n`
              )
            );
          }
          controller.enqueue(encoder.encode(`event: done\ndata: {}\n\n`));
          controller.close();
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

    if (url.pathname === "/sse-test" && request.method === "GET") {
      return new Response(
        `<!DOCTYPE html><html><head><title>SSE spike — qa-scoring</title>
<style>body{font-family:monospace;margin:2rem;background:#111;color:#ddd}
#verdict{font-size:1.4rem;padding:.5rem 1rem;border-radius:6px;display:inline-block;margin-bottom:1rem}
.wait{background:#444}.go{background:#0a5c2e}.no{background:#7a1f1f}
li{margin:.15rem 0}</style></head><body>
<h2>SSE transport spike</h2><div id="verdict" class="wait">connecting…</div>
<p>20 ticks, one every 2s. Live arrival = streaming works. All at once at the end = buffered.</p>
<ul id="log"></ul>
<script>
const t0 = Date.now(), gaps = []; let last = null;
const v = document.getElementById('verdict'), log = document.getElementById('log');
const es = new EventSource('/events/stream');
es.addEventListener('tick', e => {
  const now = Date.now();
  if (last !== null) gaps.push(now - last);
  last = now;
  const d = JSON.parse(e.data);
  log.insertAdjacentHTML('beforeend',
    '<li>tick ' + d.n + ' — arrived +' + ((now - t0)/1000).toFixed(1) + 's</li>');
  if (d.n >= 3) {
    const median = gaps.slice().sort((a,b)=>a-b)[Math.floor(gaps.length/2)];
    if (median > 1000) { v.textContent = 'STREAMING CONFIRMED — SSE works on Sandy (median gap ' + (median/1000).toFixed(1) + 's)'; v.className = 'go'; }
  }
});
es.addEventListener('done', e => {
  es.close();
  const median = gaps.slice().sort((a,b)=>a-b)[Math.floor(gaps.length/2)] || 0;
  if (median <= 1000) { v.textContent = 'BUFFERED — all ticks arrived together; SSE falls back to short-poll'; v.className = 'no'; }
});
es.onerror = () => { if (v.className === 'wait') { v.textContent = 'CONNECTION ERROR — see console'; v.className = 'no'; } };
</script></body></html>`,
        { headers: { "Content-Type": "text/html; charset=utf-8" } }
      );
    }

    // Handle form POST — insert a new item, then redirect back to the page
    if (url.pathname === "/api/items" && request.method === "POST") {
      const formData = await request.formData();
      const name = formData.get("name")?.toString().trim();
      if (name) {
        try {
          await db.insert(items).values({
            id: crypto.randomUUID(),
            name,
            created_at: new Date().toISOString(),
          });
        } catch (e: any) {
          return renderPage([], e?.message ?? String(e), [], undefined, undefined, undefined, styles);
        }
      }
      return Response.redirect(new URL("/", request.url).toString(), 303);
    }

    // POST /_sandy/cron — dispatched by the Sandy cron scheduler on each scheduled fire.
    // Sandy routes this instead of scheduled() (which is not supported on WfP workers).
    // Runs in full fetch() context: env.DB available, outbound-worker HMAC applied automatically.
    //
    // Body: { cron: "0 9 * * *", timestamp: "2026-01-01T09:00:00.000Z" }
    //
    // If you have multiple cron schedules, switch on `cron` to run different tasks:
    //
    //   const { cron } = await request.json() as { cron: string };
    //   if (cron === "0 9 * * *") await runMorningTask(db);
    //   if (cron === "0 21 * * *") await runEveningTask(db);
    //
    // To trigger a Sandy Workflow (callbacks work too — pass `request` for URL derivation):
    //
    //   await triggerWorkflow(env.WORKFLOW_ID!, { triggered_by: "cron", cron });
    //   await triggerWorkflowWithCallback(env.WORKFLOW_ID!, "my-workflow", request, { cron });
    if (url.pathname === "/_sandy/cron" && request.method === "POST") {
      if (request.headers.get("X-Sandy-Cron-Secret") !== env.SANDY_CRON_DISPATCH_SECRET) {
        return Response.json({ ok: false, error: "Unauthorized" }, { status: 401 });
      }
      const { cron } = await request.json() as { cron: string; timestamp: string };
      console.log("[cron] fired", cron, "at", new Date().toISOString());
      // TODO: add your scheduled logic here. env.DB is available for database access.
      // Switch on `cron` if you have multiple schedules doing different things.
      return Response.json({ ok: true });
    }

    // POST /api/v1/callbacks/:workflow-name — receives results POSTed back by a Sandy Workflow.
    //
    // Auth is handled transparently by the dispatch worker (HMAC verified before forwarding).
    // X-Sandy-Workflow-Callback will be "verified" on all legitimate workflow callbacks.
    //
    // The workflow's final step calls sendCallback(callback_url, result, _sandySecrets).
    // Your handler receives: { run_id, status, ...workflow-specific fields }
    //
    // IMPORTANT: you MUST persist the result to D1 here. Workers are stateless — if you
    // don't write to the database, the result is lost when this request ends and your UI
    // cannot display it. Use run_id as the primary key to correlate with the trigger response.
    //
    // workflow_runs is a short-term staging table. If you need results long-term, write them
    // to your own domain table here (e.g. evaluations, scores, listings) instead of or in
    // addition to workflow_runs. Set up a cron job to prune old workflow_runs rows:
    //   DELETE FROM workflow_runs WHERE created_at < datetime('now', '-7 days');
    if (url.pathname.startsWith("/api/v1/callbacks/") && request.method === "POST") {
      if (request.headers.get("X-Sandy-Workflow-Callback") !== "verified") {
        return Response.json({ ok: false, error: "Unauthorized" }, { status: 401 });
      }
      const workflowName = decodeURIComponent(url.pathname.replace("/api/v1/callbacks/", ""));
      const body = await request.json() as { run_id?: string; status?: string; [key: string]: unknown };
      if (!body.run_id) return Response.json({ ok: false, error: "missing run_id" }, { status: 400 });
      await db.update(workflowRuns)
        .set({ status: body.status ?? "error", result: JSON.stringify(body) })
        .where(eq(workflowRuns.run_id, body.run_id));
      console.log(`[callback] workflow=${workflowName} run_id=${body.run_id} status=${body.status}`);
      // qa-scoring-pipeline: persist the evaluation (draft/finalize + toast)
      if (workflowName === "qa-scoring-pipeline") {
        const { scoringCallback } = await import("./routes/scoring.js");
        try {
          const outcome = await scoringCallback(body, env.DB);
          const jobId = (body as any).persist
            ? `score-${(body as any).team_id}-${(body as any).call_id}-${String((body as any).persist.agent_name ?? "")
                .toLowerCase()
                .replace(/[^a-z0-9]+/g, "-")}`
            : null;
          if (jobId) {
            await env.DB.prepare(
              "UPDATE workflow_runs SET status = ?, result = ? WHERE run_id = ?"
            )
              .bind(outcome.ok ? "complete" : "error", JSON.stringify(outcome), jobId)
              .run();
          }
          console.log(`[scoring] ${outcome.ok ? "OK" : "FAIL"}: ${outcome.note}`);
        } catch (err) {
          console.log(`[scoring] persist threw: ${String(err).slice(0, 300)}`);
        }
      }
      return Response.json({ ok: true });
    }

    // POST /api/workflow/trigger — fire a Sandy Workflow in the background.
    // Supports three input modes via the hidden `input_type` field:
    //   "plain"  — trigger with JSON params
    //   "pdf"    — multipart/form-data with a `pdf` file field; converts to base64
    //   "csv"    — multipart/form-data with a `csv` textarea field
    //
    // The outbound-worker stamps X-Sandy-App and X-Sandy-Internal HMAC automatically.
    if (url.pathname === "/api/workflow/trigger" && request.method === "POST") {
      const contentType = request.headers.get("content-type") ?? "";
      let workflowId: string | null = null;
      let workflowName: string | null = null;
      let params: Record<string, unknown> = {};

      if (contentType.includes("multipart/form-data")) {
        const formData = await request.formData();
        workflowId = formData.get("workflow_id")?.toString() ?? env.WORKFLOW_ID ?? null;
        workflowName = formData.get("workflow_name")?.toString() ?? null;
        const inputType = formData.get("input_type")?.toString() ?? "plain";

        if (inputType === "pdf") {
          // PDF → base64 → workflow receives event.payload.pdf_base64
          // Workflow passes to Anthropic as:
          //   { type: "document", source: { type: "base64", media_type: "application/pdf", data: pdf_base64 } }
          const file = formData.get("pdf") as File | null;
          if (file && file.size > 0) {
            const buffer = await file.arrayBuffer();
            const bytes = new Uint8Array(buffer);
            let binary = "";
            for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
            params.pdf_base64 = btoa(binary);
            params.candidate_name = formData.get("candidate_name")?.toString() ?? "the candidate";
          }
        } else if (inputType === "csv") {
          // CSV text → workflow receives event.payload.csv_content
          // Expected columns: call_id,agent_name,transcript
          params.csv_content = formData.get("csv")?.toString() ?? "";
          params.batch_label = formData.get("batch_label")?.toString() ?? "";
        } else {
          // plain — optionally pass triggered_by + item count context
          const rows = await db.select().from(items).orderBy(desc(items.created_at)).limit(50);
          params = {
            triggered_by: "app",
            triggered_at: new Date().toISOString(),
            item_count: rows.length,
          };
        }
      }

      if (!workflowId) {
        return Response.redirect(new URL("/?trigger_error=No+workflow+selected", request.url).toString(), 303);
      }

      try {
        // Use triggerWorkflowWithCallback when the workflow name is known — this automatically
        // adds callback_url and callback_token to the params so the workflow can POST results
        // back to POST /api/v1/callbacks/{workflowName} on this app. Auth is handled transparently
        // by the dispatch-worker (HMAC verified before forwarding).
        const run = workflowName
          ? await triggerWorkflowWithCallback(workflowId, workflowName, request, params)
          : await triggerWorkflow(workflowId, params);
        if (workflowName) {
          await db.insert(workflowRuns).values({
            run_id: run.id,
            workflow_name: workflowName,
            status: "pending",
            created_at: new Date().toISOString(),
          });
        }
        return Response.redirect(new URL(`/?run_id=${run.id}`, request.url).toString(), 303);
      } catch (e: any) {
        return Response.redirect(
          new URL(`/?trigger_error=${encodeURIComponent(e?.message ?? String(e))}`, request.url).toString(),
          303
        );
      }
    }

    // Fetch workflows this app is permitted to trigger.
    let workflows: WorkflowInfo[] = [];
    try { workflows = await listTriggerableWorkflows(); } catch { /* non-fatal */ }

    const runId = url.searchParams.get("run_id") ?? undefined;
    const triggerError = url.searchParams.get("trigger_error") ?? undefined;
    let rows: { id: string; name: string; created_at: string }[] = [];
    let recentRuns: { run_id: string; workflow_name: string; status: string; result: string | null; created_at: string }[] = [];
    let error: string | undefined;
    try {
      rows = await db.select().from(items).orderBy(desc(items.created_at)).limit(50);
      recentRuns = await db.select().from(workflowRuns).orderBy(desc(workflowRuns.created_at)).limit(10);
    } catch (e: any) {
      error = e?.message ?? String(e);
    }

    return renderPage(rows, recentRuns, error, workflows, env.WORKFLOW_ID, runId, triggerError, styles);
  },
};

function renderPage(
  rows: { id: string; name: string; created_at: string }[],
  recentRuns: { run_id: string; workflow_name: string; status: string; result: string | null; created_at: string }[],
  error: string | undefined,
  workflows: WorkflowInfo[],
  defaultWorkflowId: string | undefined,
  lastRunId: string | undefined,
  triggerError: string | undefined,
  styles: string
): Response {
  const appHtml = renderToString(
    <App items={rows} recentRuns={recentRuns} error={error} workflows={workflows}
      defaultWorkflowId={defaultWorkflowId} lastRunId={lastRunId} triggerError={triggerError} />
  );
  const html = `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Sandy App</title>
  <style>${styles}</style>
</head>
<body>
  <div id="root">${appHtml}</div>
</body>
</html>`;

  return new Response(html, {
    headers: { "Content-Type": "text/html; charset=utf-8" },
  });
}

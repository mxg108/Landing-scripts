// Sandy Workflow integration helpers.
//
// The outbound-worker automatically injects X-Sandy-App and X-Sandy-Internal HMAC
// on all outbound fetch() calls — no extra auth headers needed.
//
// Cron context (/_sandy/cron):
//   Sandy dispatches POST /_sandy/cron to your app on schedule.
//   Use triggerWorkflowWithCallback(id, name, request, params) — pass the incoming
//   Request and Sandy derives the callback URL from request.url automatically.
//
// Permission gating:
//   The Sandy worker checks X-Sandy-App against the workflow's allowed_applications
//   list (or global_allow). If your app is not on the list, trigger returns 403.
//   Fix: sandy.py workflows update <id> --allowed-applications <your-app-name>
//
// Workflow callbacks:
//   Workflows POST results back to your app at:
//     POST https://{your-app}.sandy.hellolanding.tech/api/v1/callbacks/{workflow-name}
//   Use triggerWorkflowWithCallback() — works from both fetch() and /_sandy/cron contexts.
//   Add a handler for POST /api/v1/callbacks/:name in your index.tsx.
//   Auth is handled transparently — no auth code needed in your index.tsx handler.

const SANDY_API = "https://sandy.hellolanding.tech";

export interface WorkflowInfo {
  id: string;
  name: string;
  description: string | null;
  status: string;
}

export interface WorkflowRun {
  id: string;     // Sandy's run UUID — same value Cloudflare uses as the Workflow instance ID
  status: string;
}

// List workflows this app is permitted to trigger.
// Calls GET /api/v1/internal/app-workflows — HMAC-authed by the outbound-worker automatically.
export async function listTriggerableWorkflows(): Promise<WorkflowInfo[]> {
  try {
    const res = await fetch(`${SANDY_API}/api/v1/internal/app-workflows`);
    if (!res.ok) return [];
    const json = await res.json() as { ok: boolean; data: { workflows: WorkflowInfo[] } };
    return json.data.workflows;
  } catch {
    return [];
  }
}

// Trigger a workflow run with arbitrary JSON params.
export async function triggerWorkflow(
  workflowId: string,
  params: Record<string, unknown> = {}
): Promise<WorkflowRun> {
  const res = await fetch(
    `${SANDY_API}/api/v1/workflows/${workflowId}/trigger`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(params),
    }
  );
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`Workflow trigger failed ${res.status}: ${body}`);
  }
  const json = await res.json() as { ok: boolean; data: WorkflowRun };
  return json.data;
}

// Trigger a workflow that will POST results back to this app when complete.
//
// Pass the incoming Request — Sandy derives the callback URL from request.url.
// Works from both fetch() and /_sandy/cron contexts.
//
// The workflow receives:
//   event.payload.callback_url   — https://{your-app}.sandy.hellolanding.tech/api/v1/callbacks/{workflowName}
//   event.payload.callback_token — a UUID you can use to correlate the callback with this trigger
//
// In your workflow source (final step), call sendCallback() — it is defined inline in every
// workflow-template and handles HMAC signing automatically via event.payload._sandySecrets.
//
// In your app's index.tsx, handle POST /api/v1/callbacks/:name.
// The dispatch worker verifies the HMAC before forwarding — no auth code needed in your handler.
export async function triggerWorkflowWithCallback(
  workflowId: string,
  workflowName: string,
  request: Request,
  params: Record<string, unknown> = {}
): Promise<WorkflowRun & { callback_token: string }> {
  const origin = new URL(request.url).origin;
  const callback_url = `${origin}/api/v1/callbacks/${encodeURIComponent(workflowName)}`;
  const callback_token = crypto.randomUUID();
  const run = await triggerWorkflow(workflowId, { ...params, callback_url, callback_token });
  return { ...run, callback_token };
}

// Trigger a workflow with a PDF file (e.g. ai-resume-eval).
// Reads the PDF from a File object, converts to base64, passes as pdf_base64.
// The workflow receives event.payload.pdf_base64 and passes it to Anthropic as a
// document block: { type: "document", source: { type: "base64", media_type: "application/pdf", data: pdf_base64 } }
export async function triggerWorkflowWithPdf(
  workflowId: string,
  pdfFile: File,
  extraParams: Record<string, unknown> = {}
): Promise<WorkflowRun> {
  const buffer = await pdfFile.arrayBuffer();
  const bytes = new Uint8Array(buffer);
  let binary = "";
  for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
  const pdf_base64 = btoa(binary);

  return triggerWorkflow(workflowId, { pdf_base64, ...extraParams });
}

// Trigger a workflow with CSV text (e.g. ai-call-transcript-scores).
// The workflow receives event.payload.csv_content as a raw CSV string.
export async function triggerWorkflowWithCsv(
  workflowId: string,
  csvContent: string,
  extraParams: Record<string, unknown> = {}
): Promise<WorkflowRun> {
  return triggerWorkflow(workflowId, { csv_content: csvContent, ...extraParams });
}

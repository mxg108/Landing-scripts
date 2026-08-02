// Root React component — receives data from the server, no client-side state needed.
// Edit this to build your prototype. Styled with the Landing design system
// (src/design-system + src/brand).

import type { WorkflowInfo } from "./workflow.js";
import { Navbar } from "./brand/Navbar.js";
import { Footer } from "./brand/Footer.js";
import { Icon } from "./brand/Icon.js";

interface Item {
  id: string;
  name: string;
  created_at: string;
}

interface WorkflowRun {
  run_id: string;
  workflow_name: string;
  status: string;
  result: string | null;
  created_at: string;
}

interface Props {
  items: Item[];
  recentRuns: WorkflowRun[];
  error?: string;
  workflows: WorkflowInfo[];
  defaultWorkflowId?: string;
  lastRunId?: string;
  triggerError?: string;
}

export default function App({ items, recentRuns, error, workflows, defaultWorkflowId, lastRunId, triggerError }: Props) {
  const selectedId = defaultWorkflowId ?? workflows[0]?.id ?? "";
  // Name of the default-selected workflow — used to build the callback URL path.
  // For multi-workflow dropdowns, sync this hidden field to the selected option in your real UI.
  const selectedWorkflowName = workflows.find(w => w.id === selectedId)?.name ?? selectedId;

  return (
    <div className="min-h-screen flex flex-col" style={{ background: "var(--bg-secondary)" }}>
      <Navbar />

      {/* Hero */}
      <section className="ds-container">
        <div
          className="ds-content text-center flex flex-col items-center gap-4"
          style={{ paddingTop: "var(--space-9)", paddingBottom: "var(--space-7)" }}
        >
          <span
            className="label-sm"
            style={{ color: "var(--landing-bright-blue)", textTransform: "uppercase", letterSpacing: "0.06em" }}
          >
            Sandy App · D1 Database
          </span>
          <h1 className="display-md" style={{ margin: 0 }}>Where every stay feels like home.</h1>
          <p className="body-lg" style={{ color: "var(--text-secondary)", maxWidth: 560, margin: 0 }}>
            Edit <code>src/App.tsx</code> to build your prototype. Data is persisted to D1 and
            processed with Sandy Workflows.
          </p>
        </div>
      </section>

      <main className="ds-container flex-1">
        <div
          className="ds-content w-full max-w-xl mx-auto flex flex-col gap-6"
          style={{ paddingBottom: "var(--space-9)" }}
        >

          {/* ── Items (D1) ────────────────────────────────────────────────────── */}
          <div className="ds-card" style={{ padding: "var(--space-5)" }}>
            <div className="flex items-center gap-2" style={{ marginBottom: "var(--space-3)" }}>
              <span style={{ color: "var(--landing-bright-blue)" }}><Icon name="house" size="sm" /></span>
              <h2 className="header-xs" style={{ margin: 0, color: "var(--landing-blue)" }}>Items</h2>
            </div>
            <form method="POST" action="/api/items" className="flex gap-2" style={{ marginBottom: "var(--space-4)" }}>
              <input type="text" name="name" placeholder="New item name" className="ds-input" style={{ flex: 1, height: 40 }} />
              <button type="submit" className="btn btn-primary btn-sm">Add</button>
            </form>

            {error ? (
              <p className="body-sm" style={{ color: "var(--status-error-text)", margin: 0 }}>Error: {error}</p>
            ) : items.length === 0 ? (
              <p className="body-sm text-center" style={{ color: "var(--text-tertiary)", margin: 0 }}>
                No items yet — add one above.
              </p>
            ) : (
              <ul style={{ listStyle: "none", margin: 0, padding: 0, border: "1px solid var(--border-secondary)", borderRadius: "var(--radius-sm)" }}>
                {items.map((item, i) => (
                  <li
                    key={item.id}
                    className="flex items-baseline gap-2"
                    style={{ padding: "var(--space-3) var(--space-4)", borderTop: i === 0 ? "none" : "1px solid var(--border-secondary)" }}
                  >
                    <span className="body-sm" style={{ color: "var(--text-primary)" }}>{item.name}</span>
                    <span className="body-xs" style={{ color: "var(--text-tertiary)" }}>{item.created_at.slice(0, 10)}</span>
                  </li>
                ))}
              </ul>
            )}
          </div>

          {/* ── Sandy Workflow integration ────────────────────────────────────────
              Common pattern: app writes data to D1, then triggers a workflow to process
              it — AI scoring, external sync, PDF evaluation, CSV batch jobs, etc.

              How auth works:
                - outbound-worker injects X-Sandy-App + X-Sandy-Internal HMAC automatically
                - Sandy worker checks X-Sandy-App against workflow's allowed_applications
                - 403? → sandy.py workflows update <id> --allowed-applications <app-name>

              WfP + workflow-dispatcher:
                App → outbound-worker (WfP) → Sandy API → service binding → workflow-dispatcher
                workflow-dispatcher is NOT in WfP namespace — called via service binding only.

              Workflow callbacks:
                Workflows POST results back here via POST /api/v1/callbacks/{workflow-name}.
                The dispatch-worker verifies the HMAC before forwarding — this app handles no auth.
                The trigger handler passes callback_url automatically when workflow_name is
                included in the form. Results are POSTed back and persisted to D1.
                See index.tsx POST /api/v1/callbacks/*.
          */}
          <div className="ds-card" style={{ padding: "var(--space-5)" }}>
            <div className="flex items-center gap-2" style={{ marginBottom: "var(--space-4)" }}>
              <span style={{ color: "var(--landing-bright-blue)" }}><Icon name="refresh" size="sm" /></span>
              <h2 className="header-xs" style={{ margin: 0, color: "var(--landing-blue)" }}>Sandy Workflows</h2>
            </div>

            {/* Available workflows list */}
            {workflows.length > 0 ? (
              <div style={{ border: "1px solid var(--border-secondary)", borderRadius: "var(--radius-sm)", marginBottom: "var(--space-4)" }}>
                <ul style={{ listStyle: "none", margin: 0, padding: 0 }}>
                  {workflows.map((wf, i) => (
                    <li
                      key={wf.id}
                      className="flex items-center gap-3"
                      style={{ padding: "var(--space-2) var(--space-4)", borderTop: i === 0 ? "none" : "1px solid var(--border-secondary)" }}
                    >
                      <span
                        className="inline-block rounded-pill"
                        style={{ width: 8, height: 8, flexShrink: 0, background: wf.status === "ready" ? "var(--status-success-page)" : "var(--landing-light-grey)" }}
                      />
                      <span className="body-sm flex-1" style={{ color: "var(--text-primary)" }}>{wf.name}</span>
                      <code className="truncate" style={{ maxWidth: 120, color: "var(--text-tertiary)", fontSize: "var(--label-xs)" }}>{wf.id}</code>
                    </li>
                  ))}
                </ul>
              </div>
            ) : (
              <p className="body-xs" style={{ color: "var(--text-tertiary)", marginBottom: "var(--space-4)" }}>
                No triggerable workflows. Create one and add this app to its allow-list.
              </p>
            )}

            {/* Trigger — plain (passes item count as context) */}
            <form method="POST" action="/api/workflow/trigger" encType="multipart/form-data" className="flex gap-2">
              <input type="hidden" name="input_type" value="plain" />
              <input type="hidden" name="workflow_name" value={selectedWorkflowName} />
              {workflows.length > 1 ? (
                <select name="workflow_id" defaultValue={selectedId} className="ds-input" style={{ flex: 1, height: 40 }}>
                  {workflows.map(wf => <option key={wf.id} value={wf.id}>{wf.name}</option>)}
                </select>
              ) : (
                <input type="hidden" name="workflow_id" value={selectedId} />
              )}
              <button type="submit" disabled={!selectedId} className="btn btn-primary btn-sm">Trigger</button>
            </form>

            {/* Trigger — PDF file (for ai-resume-eval style workflows) */}
            <details style={{ marginTop: "var(--space-3)", border: "1px solid var(--border-primary)", borderRadius: "var(--radius-sm)" }}>
              <summary className="label-sm" style={{ padding: "var(--space-2) var(--space-4)", color: "var(--landing-blue)", cursor: "pointer" }}>
                Trigger with PDF → <code>event.payload.pdf_base64</code>
              </summary>
              <form method="POST" action="/api/workflow/trigger" encType="multipart/form-data" className="flex flex-col gap-2" style={{ padding: "0 var(--space-4) var(--space-4)" }}>
                <input type="hidden" name="input_type" value="pdf" />
                <input type="hidden" name="workflow_id" value={selectedId} />
                <input type="hidden" name="workflow_name" value={selectedWorkflowName} />
                <p className="body-xs" style={{ color: "var(--text-secondary)" }}>
                  PDF is converted to base64 and passed as <code>pdf_base64</code>.
                  Workflow passes it to Anthropic as a document block.
                </p>
                <input type="text" name="candidate_name" placeholder="Candidate name (optional)" className="ds-input" style={{ height: 40 }} />
                <input type="file" name="pdf" accept="application/pdf" className="body-sm" style={{ color: "var(--text-secondary)" }} />
                <button type="submit" disabled={!selectedId} className="btn btn-primary btn-sm">Upload &amp; trigger</button>
              </form>
            </details>

            {/* Trigger — CSV text (for ai-call-transcript-scores style workflows) */}
            <details style={{ marginTop: "var(--space-3)", border: "1px solid var(--border-primary)", borderRadius: "var(--radius-sm)" }}>
              <summary className="label-sm" style={{ padding: "var(--space-2) var(--space-4)", color: "var(--landing-blue)", cursor: "pointer" }}>
                Trigger with CSV → <code>event.payload.csv_content</code>
              </summary>
              <form method="POST" action="/api/workflow/trigger" encType="multipart/form-data" className="flex flex-col gap-2" style={{ padding: "0 var(--space-4) var(--space-4)" }}>
                <input type="hidden" name="input_type" value="csv" />
                <input type="hidden" name="workflow_id" value={selectedId} />
                <input type="hidden" name="workflow_name" value={selectedWorkflowName} />
                <p className="body-xs" style={{ color: "var(--text-secondary)" }}>
                  CSV text passed as <code>csv_content</code>.
                  Columns: <code>call_id,agent_name,transcript</code>
                </p>
                <input type="text" name="batch_label" placeholder="Batch label (optional)" className="ds-input" style={{ height: 40 }} />
                <textarea name="csv" rows={3}
                  placeholder={"call_id,agent_name,transcript\nC001,Alice,\"Customer: ...\"\nC002,Bob,\"Customer: ...\""}
                  className="ds-input font-mono" style={{ fontSize: "var(--label-xs)" }} />
                <button type="submit" disabled={!selectedId} className="btn btn-primary btn-sm">Submit CSV &amp; trigger</button>
              </form>
            </details>

            {lastRunId && (
              <div
                className="flex items-start gap-2 body-sm"
                style={{
                  marginTop: "var(--space-3)",
                  color: "var(--status-success-text)", background: "var(--status-success-bg)",
                  border: "1px solid var(--border-success)", borderRadius: "var(--radius-sm)",
                  padding: "var(--space-2) var(--space-3)", wordBreak: "break-all",
                }}
              >
                <span style={{ flexShrink: 0 }}><Icon name="circle-check" size="sm" /></span>
                <span>Queued: <code>{lastRunId}</code></span>
              </div>
            )}

            {/* Recent workflow runs — persisted to D1 by the callback handler */}
            {recentRuns.length > 0 && (
              <div className="flex flex-col gap-2" style={{ marginTop: "var(--space-4)" }}>
                <h3 className="label-xs" style={{ color: "var(--text-secondary)", textTransform: "uppercase", letterSpacing: "0.04em", margin: 0 }}>
                  Recent runs
                </h3>
                <ul style={{ listStyle: "none", margin: 0, padding: 0, border: "1px solid var(--border-secondary)", borderRadius: "var(--radius-sm)" }}>
                  {recentRuns.map((run, i) => (
                    <li
                      key={run.run_id}
                      className="flex items-start gap-2"
                      style={{ padding: "var(--space-2) var(--space-3)", borderTop: i === 0 ? "none" : "1px solid var(--border-secondary)" }}
                    >
                      <span
                        className="inline-block rounded-pill"
                        style={{
                          marginTop: 4, width: 8, height: 8, flexShrink: 0,
                          background:
                            run.status === "complete" ? "var(--status-success-page)" :
                            run.status === "error" ? "var(--status-error-page)" : "var(--status-warning-page)",
                        }}
                      />
                      <div className="flex-1 min-w-0">
                        <div className="flex gap-2 items-baseline">
                          <span className="label-xs" style={{ color: "var(--text-primary)" }}>{run.workflow_name}</span>
                          <span className="body-xs" style={{ color: "var(--text-tertiary)" }}>{run.status}</span>
                          <span className="body-xs" style={{ color: "var(--text-tertiary)", marginLeft: "auto" }}>{run.created_at.slice(0, 10)}</span>
                        </div>
                        {run.result && (
                          <pre className="body-xs" style={{ marginTop: 4, color: "var(--text-secondary)", whiteSpace: "pre-wrap", wordBreak: "break-all" }}>
                            {run.result.slice(0, 200)}
                          </pre>
                        )}
                      </div>
                    </li>
                  ))}
                </ul>
              </div>
            )}
            {triggerError && (
              <p
                className="body-sm"
                style={{
                  marginTop: "var(--space-3)",
                  color: "var(--status-error-text)", background: "var(--status-error-bg)",
                  border: "1px solid var(--border-error)", borderRadius: "var(--radius-sm)",
                  padding: "var(--space-2) var(--space-3)",
                }}
              >
                {triggerError}
              </p>
            )}
          </div>
        </div>
      </main>

      <Footer />
    </div>
  );
}

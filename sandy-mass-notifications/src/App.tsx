// mass-notifications UI — SSR, Landing design system.
// Pages: access (no role), home (campaign list + admin), campaign (detail grid).

import { Navbar } from "./brand/Navbar.js";
import { Footer } from "./brand/Footer.js";
import { Icon } from "./brand/Icon.js";

export interface CampaignRow {
  id: string; mode: string; property_name: string; event_name: string;
  status: string; fetch_stats_json: string | null;
  created_by: string; created_at: string;
}

export interface RecipientRow {
  id: string; campaign_id: string; reservation_id: string | null;
  email: string; name: string; unit: string;
  phone_e164: string | null; segment_timezone: string | null;
  agm_name: string | null; source: string; status: string; notes: string;
}

export interface RoleRow { email: string; role: string; created_at: string }

export interface AppProps {
  page: "access" | "home" | "campaign";
  user: { email: string; username: string };
  role: string | null;
  campaigns: CampaignRow[];
  recipients: RecipientRow[];
  roleRequests: RoleRow[];
  flash?: string;
  error?: string;
}

const STATUS_COLORS: Record<string, { bg: string; text: string }> = {
  PENDING: { bg: "var(--status-info-bg)", text: "var(--status-info-text)" },
  READY: { bg: "var(--status-success-bg)", text: "var(--status-success-text)" },
  REVIEW: { bg: "var(--status-error-bg)", text: "var(--status-error-text)" },
  SENT: { bg: "var(--bg-tertiary)", text: "var(--text-tertiary)" },
  DRAFT: { bg: "var(--status-warning-bg)", text: "var(--status-warning-text)" },
};

function StatusChip({ status }: { status: string }) {
  const s = status || "PENDING";
  const c = STATUS_COLORS[s] ?? STATUS_COLORS.PENDING;
  return (
    <span className="label-xs rounded-pill" style={{
      background: c.bg, color: c.text, padding: "2px 10px", whiteSpace: "nowrap",
    }}>{s}</span>
  );
}

function CampaignStatusChip({ status }: { status: string }) {
  const map: Record<string, { bg: string; text: string }> = {
    draft: { bg: "var(--bg-tertiary)", text: "var(--text-secondary)" },
    fetching: { bg: "var(--status-warning-bg)", text: "var(--status-warning-text)" },
    ready: { bg: "var(--status-success-bg)", text: "var(--status-success-text)" },
    errored: { bg: "var(--status-error-bg)", text: "var(--status-error-text)" },
    sending: { bg: "var(--status-warning-bg)", text: "var(--status-warning-text)" },
    complete: { bg: "var(--status-success-bg)", text: "var(--status-success-text)" },
  };
  const c = map[status] ?? map.draft;
  return (
    <span className="label-xs rounded-pill" style={{
      background: c.bg, color: c.text, padding: "2px 10px", whiteSpace: "nowrap",
    }}>{status}</span>
  );
}

function Banner({ kind, children }: { kind: "success" | "error"; children: any }) {
  const success = kind === "success";
  return (
    <div className="flex items-start gap-2 body-sm" style={{
      color: success ? "var(--status-success-text)" : "var(--status-error-text)",
      background: success ? "var(--status-success-bg)" : "var(--status-error-bg)",
      border: `1px solid ${success ? "var(--border-success)" : "var(--border-error)"}`,
      borderRadius: "var(--radius-sm)", padding: "var(--space-2) var(--space-3)",
      marginBottom: "var(--space-4)",
    }}>
      <span style={{ flexShrink: 0 }}><Icon name={success ? "circle-check" : "more-info"} size="sm" /></span>
      <span>{children}</span>
    </div>
  );
}

function AccessPage({ user, role }: { user: AppProps["user"]; role: string | null }) {
  return (
    <main className="ds-container flex-1">
      <div className="ds-content w-full max-w-md mx-auto text-center flex flex-col items-center gap-4"
        style={{ paddingTop: "var(--space-9)", paddingBottom: "var(--space-9)" }}>
        <h1 className="header-lg" style={{ margin: 0 }}>Mass Notifications</h1>
        {role === "requested" ? (
          <>
            <p className="body-md" style={{ color: "var(--text-secondary)", margin: 0 }}>
              Access request pending for <strong>{user.email}</strong>. An admin will review it shortly.
            </p>
          </>
        ) : (
          <>
            <p className="body-md" style={{ color: "var(--text-secondary)", margin: 0 }}>
              Signed in as <strong>{user.email}</strong> — this account doesn't have access yet.
            </p>
            <form method="POST" action="/api/request-access">
              <button type="submit" className="btn btn-primary">Request access</button>
            </form>
          </>
        )}
      </div>
    </main>
  );
}

function HomePage({ user, role, campaigns, roleRequests, flash, error }: AppProps) {
  const pending = roleRequests.filter((r) => r.role === "requested");
  const granted = roleRequests.filter((r) => r.role !== "requested");
  return (
    <main className="ds-container flex-1">
      <div className="ds-content w-full max-w-2xl mx-auto flex flex-col gap-6"
        style={{ paddingTop: "var(--space-7)", paddingBottom: "var(--space-9)" }}>
        <div className="text-center flex flex-col items-center gap-2">
          <span className="label-sm" style={{ color: "var(--landing-bright-blue)", textTransform: "uppercase", letterSpacing: "0.06em" }}>
            Member Support · Property Notifications
          </span>
          <h1 className="display-sm" style={{ margin: 0 }}>Mass Notifications</h1>
        </div>

        {flash && <Banner kind="success">{flash}</Banner>}
        {error && <Banner kind="error">{error}</Banner>}

        {/* New campaign */}
        <div className="ds-card" style={{ padding: "var(--space-5)" }}>
          <div className="flex items-center gap-2" style={{ marginBottom: "var(--space-3)" }}>
            <span style={{ color: "var(--landing-bright-blue)" }}><Icon name="house" size="sm" /></span>
            <h2 className="header-xs" style={{ margin: 0, color: "var(--landing-blue)" }}>New campaign</h2>
          </div>
          <form method="POST" action="/api/campaigns" className="flex gap-2 flex-wrap">
            <input type="text" name="property_name" required placeholder="Property name (exact, e.g. Woodhill)"
              className="ds-input" style={{ flex: 2, minWidth: 200, height: 40 }} />
            <input type="text" name="event_name" placeholder="Event (e.g. Fire Inspection)"
              className="ds-input" style={{ flex: 2, minWidth: 180, height: 40 }} />
            <button type="submit" className="btn btn-primary btn-sm">Create</button>
          </form>
          <p className="body-xs" style={{ color: "var(--text-tertiary)", margin: "var(--space-2) 0 0" }}>
            Property name must match the warehouse exactly — same value as the Sigma
            "Member Information/Emails" Property Name filter.
          </p>
        </div>

        {/* Campaign list */}
        <div className="ds-card" style={{ padding: "var(--space-5)" }}>
          <div className="flex items-center gap-2" style={{ marginBottom: "var(--space-3)" }}>
            <span style={{ color: "var(--landing-bright-blue)" }}><Icon name="refresh" size="sm" /></span>
            <h2 className="header-xs" style={{ margin: 0, color: "var(--landing-blue)" }}>Campaigns</h2>
          </div>
          {campaigns.length === 0 ? (
            <p className="body-sm text-center" style={{ color: "var(--text-tertiary)", margin: 0 }}>
              No campaigns yet — create one above.
            </p>
          ) : (
            <ul style={{ listStyle: "none", margin: 0, padding: 0, border: "1px solid var(--border-secondary)", borderRadius: "var(--radius-sm)" }}>
              {campaigns.map((c, i) => (
                <li key={c.id} style={{ borderTop: i === 0 ? "none" : "1px solid var(--border-secondary)" }}>
                  <a href={`/c/${c.id}`} className="flex items-center gap-3"
                    style={{ padding: "var(--space-3) var(--space-4)", textDecoration: "none" }}>
                    <div className="flex-1 min-w-0">
                      <div className="label-sm" style={{ color: "var(--text-primary)" }}>
                        {c.property_name}{c.event_name ? ` — ${c.event_name}` : ""}
                      </div>
                      <div className="body-xs" style={{ color: "var(--text-tertiary)" }}>
                        {c.mode} · {c.created_by.split("@")[0]} · {c.created_at.slice(0, 10)}
                      </div>
                    </div>
                    <CampaignStatusChip status={c.status} />
                  </a>
                </li>
              ))}
            </ul>
          )}
        </div>

        {/* Admin: access management */}
        {role === "admin" && (
          <div className="ds-card" style={{ padding: "var(--space-5)" }}>
            <h2 className="header-xs" style={{ margin: "0 0 var(--space-3)", color: "var(--landing-blue)" }}>Access</h2>
            {pending.length > 0 && (
              <div style={{ marginBottom: "var(--space-4)" }}>
                <h3 className="label-xs" style={{ color: "var(--text-secondary)", textTransform: "uppercase", margin: "0 0 var(--space-2)" }}>
                  Pending requests
                </h3>
                {pending.map((r) => (
                  <form key={r.email} method="POST" action="/api/roles" className="flex items-center gap-2"
                    style={{ marginBottom: "var(--space-2)" }}>
                    <input type="hidden" name="email" value={r.email} />
                    <span className="body-sm flex-1" style={{ color: "var(--text-primary)" }}>{r.email}</span>
                    <button type="submit" name="role" value="operator" className="btn btn-primary btn-sm">Operator</button>
                    <button type="submit" name="role" value="admin" className="btn btn-secondary btn-sm">Admin</button>
                    <button type="submit" name="role" value="denied" className="btn btn-tertiary btn-sm">Deny</button>
                  </form>
                ))}
              </div>
            )}
            <details>
              <summary className="label-sm" style={{ color: "var(--landing-blue)", cursor: "pointer" }}>
                {granted.length} member{granted.length === 1 ? "" : "s"} with access
              </summary>
              <ul style={{ listStyle: "none", margin: "var(--space-2) 0 0", padding: 0 }}>
                {granted.map((r) => (
                  <li key={r.email} className="flex items-center gap-2 body-sm" style={{ padding: "var(--space-1) 0", color: "var(--text-secondary)" }}>
                    <span className="flex-1">{r.email}</span>
                    <span className="label-xs" style={{ color: "var(--text-tertiary)" }}>{r.role}</span>
                  </li>
                ))}
              </ul>
            </details>
          </div>
        )}
      </div>
    </main>
  );
}

function CampaignPage({ campaigns, recipients, flash, error }: AppProps) {
  const c = campaigns[0];
  const eligible = recipients.filter((r) => ["", "PENDING", "READY"].includes(r.status)).length;
  const review = recipients.filter((r) => r.status === "REVIEW").length;
  const smsReady = recipients.filter((r) => r.phone_e164).length;
  let stats: Record<string, unknown> | null = null;
  try { stats = c.fetch_stats_json ? JSON.parse(c.fetch_stats_json) : null; } catch { /* ignore */ }

  return (
    <main className="ds-container flex-1">
      <div className="ds-content w-full max-w-3xl mx-auto flex flex-col gap-5"
        style={{ paddingTop: "var(--space-6)", paddingBottom: "var(--space-9)" }}>

        <div className="flex items-center gap-3 flex-wrap">
          <a href="/" className="label-sm flex items-center gap-1" style={{ color: "var(--landing-bright-blue)", textDecoration: "none" }}>
            <Icon name="arrow-short-left" size="sm" /> All campaigns
          </a>
          <span className="flex-1" />
          <CampaignStatusChip status={c.status} />
        </div>

        <div>
          <h1 className="header-lg" style={{ margin: 0 }}>
            {c.property_name}{c.event_name ? ` — ${c.event_name}` : ""}
          </h1>
          <p className="body-sm" style={{ color: "var(--text-tertiary)", margin: "var(--space-1) 0 0" }}>
            {c.mode} · created by {c.created_by} · {c.created_at.slice(0, 10)}
          </p>
        </div>

        {flash && <Banner kind="success">{flash}</Banner>}
        {error && <Banner kind="error">{error}</Banner>}
        {c.status === "errored" && stats?.error ? <Banner kind="error">Fetch failed: {String(stats.error)}</Banner> : null}

        {/* Recipients toolbar */}
        <div className="ds-card" style={{ padding: "var(--space-5)" }}>
          <div className="flex items-center gap-3 flex-wrap" style={{ marginBottom: "var(--space-3)" }}>
            <h2 className="header-xs" style={{ margin: 0, color: "var(--landing-blue)" }}>
              Recipients
            </h2>
            <span className="body-xs" style={{ color: "var(--text-tertiary)" }}>
              {recipients.length} total · {eligible} eligible · {review} review · {smsReady} with SMS-ready phone
            </span>
            <span className="flex-1" />
            <form method="POST" action={`/api/campaigns/${c.id}/fetch`}>
              <button type="submit" className="btn btn-primary btn-sm"
                disabled={c.status === "fetching"}>
                {recipients.some((r) => r.source === "warehouse") ? "Re-fetch active residents" : "Fetch active residents"}
              </button>
            </form>
          </div>
          {c.status === "fetching" && (
            <p className="body-sm" style={{ color: "var(--status-warning-text)", margin: "0 0 var(--space-3)" }}>
              Fetching from the warehouse — refresh this page in a few seconds.
            </p>
          )}

          {/* Manual add */}
          <form method="POST" action={`/api/campaigns/${c.id}/recipients`} className="flex gap-2 flex-wrap"
            style={{ marginBottom: "var(--space-4)" }}>
            <input type="email" name="email" required placeholder="email@example.com" className="ds-input" style={{ flex: 2, minWidth: 180, height: 36 }} />
            <input type="text" name="name" placeholder="Name" className="ds-input" style={{ flex: 2, minWidth: 140, height: 36 }} />
            <input type="text" name="unit" placeholder="Unit" className="ds-input" style={{ flex: 1, minWidth: 80, height: 36 }} />
            <button type="submit" className="btn btn-secondary btn-sm">Add manually</button>
          </form>

          {recipients.length === 0 ? (
            <p className="body-sm text-center" style={{ color: "var(--text-tertiary)", margin: 0 }}>
              No recipients yet — fetch active residents or add manually.
            </p>
          ) : (
            <div style={{ overflowX: "auto", border: "1px solid var(--border-secondary)", borderRadius: "var(--radius-sm)" }}>
              <table style={{ width: "100%", borderCollapse: "collapse" }}>
                <thead>
                  <tr style={{ background: "var(--bg-tertiary)" }}>
                    {["Unit", "Name", "Email", "Phone (E.164)", "Status", "Notes", ""].map((h) => (
                      <th key={h} className="label-xs" style={{
                        textAlign: "left", padding: "var(--space-2) var(--space-3)",
                        color: "var(--text-secondary)", whiteSpace: "nowrap",
                      }}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {recipients.map((r) => (
                    <tr key={r.id} style={{ borderTop: "1px solid var(--border-secondary)" }}>
                      <td className="body-sm" style={{ padding: "var(--space-2) var(--space-3)", whiteSpace: "nowrap" }}>{r.unit}</td>
                      <td className="body-sm" style={{ padding: "var(--space-2) var(--space-3)" }}>{r.name}</td>
                      <td className="body-sm" style={{ padding: "var(--space-2) var(--space-3)", wordBreak: "break-all" }}>{r.email}</td>
                      <td className="body-sm" style={{ padding: "var(--space-2) var(--space-3)", whiteSpace: "nowrap", color: r.phone_e164 ? "var(--text-primary)" : "var(--text-tertiary)" }}>
                        {r.phone_e164 ?? "—"}
                      </td>
                      <td style={{ padding: "var(--space-2) var(--space-3)" }}>
                        <form method="POST" action={`/api/recipients/${r.id}`} className="flex items-center gap-1">
                          <input type="hidden" name="action" value="status" />
                          <StatusChip status={r.status} />
                          <select name="status" defaultValue={r.status || "PENDING"} className="ds-input"
                            style={{ height: 28, fontSize: "var(--label-xs)", padding: "0 var(--space-1)" }}>
                            <option value="PENDING">PENDING</option>
                            <option value="READY">READY</option>
                            <option value="REVIEW">REVIEW</option>
                          </select>
                          <button type="submit" className="btn btn-tertiary btn-sm" style={{ height: 28, padding: "0 8px" }}>Set</button>
                        </form>
                      </td>
                      <td className="body-xs" style={{ padding: "var(--space-2) var(--space-3)", color: "var(--text-tertiary)", maxWidth: 200 }}>{r.notes}</td>
                      <td style={{ padding: "var(--space-2) var(--space-3)" }}>
                        <form method="POST" action={`/api/recipients/${r.id}`}>
                          <input type="hidden" name="action" value="delete" />
                          <button type="submit" className="btn btn-tertiary btn-sm" style={{ height: 28, padding: "0 8px" }}
                            title="Remove recipient">✕</button>
                        </form>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <p className="body-xs" style={{ color: "var(--text-tertiary)", margin: "var(--space-3) 0 0" }}>
            Eligible statuses: blank, PENDING, READY. Re-fetch replaces warehouse rows; manual rows are kept.
            Configure &amp; send arrives in the next release — this build is the recipient pipeline (P1).
          </p>
        </div>
      </div>
    </main>
  );
}

export default function App(props: AppProps) {
  return (
    <div className="min-h-screen flex flex-col" style={{ background: "var(--bg-secondary)" }}>
      <Navbar />
      {props.page === "access" ? <AccessPage user={props.user} role={props.role} />
        : props.page === "campaign" ? <CampaignPage {...props} />
        : <HomePage {...props} />}
      <Footer />
    </div>
  );
}

// mass-notifications UI — SSR, Landing design system.
// Pages: access (no role), home (campaign list + admin), campaign
// (recipients grid + configure + preview + send).

import { Navbar } from "./brand/Navbar.js";
import { Footer } from "./brand/Footer.js";
import { Icon } from "./brand/Icon.js";
import { EMAIL_TEMPLATES, entitiesToEmoji, type CampaignConfig } from "./emailkit.js";

// A D1-backed editable email asset (templates table, kind='card'|'disclaimer').
export interface AssetRow {
  id: string; kind: string; name: string; active: boolean;
  config: Record<string, string>;
  updated_by: string | null; updated_at: string | null;
}

export interface CampaignRow {
  id: string; mode: string; property_name: string; event_name: string;
  status: string; fetch_stats_json: string | null;
  sms_enabled: number; sms_preview_text: string | null; sms_preview_truncated: number;
  created_by: string; created_at: string;
}

export interface RecipientRow {
  id: string; campaign_id: string; reservation_id: string | null;
  email: string; name: string; unit: string;
  phone_e164: string | null; segment_timezone: string | null;
  agm_name: string | null; source: string; status: string; notes: string;
  email_state: string; email_sent_at: string | null;
  sms_state: string; sms_error: string | null;
}

export interface RoleRow { email: string; role: string; created_at: string }

export interface RunRow {
  id: string; campaign_id: string; kind: string; actor: string; count: number;
  started_at: string; completed_at: string | null; error: string | null;
}

export interface AppProps {
  page: "access" | "home" | "campaign" | "edit" | "editor";
  user: { email: string; username: string };
  role: string | null;
  campaigns: CampaignRow[];
  recipients: RecipientRow[];
  roleRequests: RoleRow[];
  runs: RunRow[];
  cards?: AssetRow[];
  disclaimers?: AssetRow[];
  editorKind?: "card" | "disclaimer";
  editing?: AssetRow;
  editorPreviewHtml?: string;
  config?: CampaignConfig;
  previewHtml?: string;
  previewSubject?: string;
  previewFor?: string;
  previewRecipientId?: string;
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

function SectionCard({ icon, title, children }: { icon: string; title: string; children: any }) {
  return (
    <div className="ds-card" style={{ padding: "var(--space-5)" }}>
      <div className="flex items-center gap-2" style={{ marginBottom: "var(--space-3)" }}>
        <span style={{ color: "var(--landing-bright-blue)" }}><Icon name={icon as any} size="sm" /></span>
        <h2 className="header-xs" style={{ margin: 0, color: "var(--landing-blue)" }}>{title}</h2>
      </div>
      {children}
    </div>
  );
}

const inputStyle = { height: 36 } as const;
const labelStyle = {
  color: "var(--text-secondary)", display: "block", marginBottom: 2,
} as const;

function Field({ label, children, grow }: { label: string; children: any; grow?: number }) {
  return (
    <label className="label-xs" style={{ ...labelStyle, flex: grow ?? 1, minWidth: 160 }}>
      {label}
      {children}
    </label>
  );
}

function AccessPage({ user, role }: { user: AppProps["user"]; role: string | null }) {
  return (
    <main className="ds-container flex-1">
      <div className="ds-content w-full max-w-md mx-auto text-center flex flex-col items-center gap-4"
        style={{ paddingTop: "var(--space-9)", paddingBottom: "var(--space-9)" }}>
        <h1 className="header-lg" style={{ margin: 0 }}>Mass Notifications</h1>
        {role === "requested" ? (
          <p className="body-md" style={{ color: "var(--text-secondary)", margin: 0 }}>
            Access request pending for <strong>{user.email}</strong>. An admin will review it shortly.
          </p>
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

function HomePage({ role, campaigns, roleRequests, flash, error }: AppProps) {
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

        <SectionCard icon="house" title="New campaign">
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
        </SectionCard>

        <SectionCard icon="refresh" title="Campaigns">
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
        </SectionCard>

        {role === "admin" && (
          <SectionCard icon="more-info" title="Access">
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
          </SectionCard>
        )}
      </div>
    </main>
  );
}

function RecipientsSection({ c, recipients }: { c: CampaignRow; recipients: RecipientRow[] }) {
  const eligible = recipients.filter((r) => ["", "PENDING", "READY"].includes(r.status)).length;
  const review = recipients.filter((r) => r.status === "REVIEW").length;
  const smsReady = recipients.filter((r) => r.phone_e164).length;
  return (
    <SectionCard icon="house" title="1 · Recipients">
      <div className="flex items-center gap-3 flex-wrap" style={{ marginBottom: "var(--space-3)" }}>
        <span className="body-xs" style={{ color: "var(--text-tertiary)" }}>
          {recipients.length} total · {eligible} eligible · {review} review · {smsReady} with SMS-ready phone
        </span>
        <span className="flex-1" />
        <form method="POST" action={`/api/campaigns/${c.id}/fetch`}>
          <button type="submit" className="btn btn-primary btn-sm" disabled={c.status === "fetching" || c.status === "sending"}>
            {recipients.some((r) => r.source === "warehouse") ? "Re-fetch active residents" : "Fetch active residents"}
          </button>
        </form>
      </div>
      {c.status === "fetching" && (
        <p className="body-sm" style={{ color: "var(--status-warning-text)", margin: "0 0 var(--space-3)" }}>
          Fetching from the warehouse — refresh this page in a few seconds.
        </p>
      )}

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
        <div style={{ overflowX: "auto", border: "1px solid var(--border-secondary)", borderRadius: "var(--radius-sm)", maxHeight: 420, overflowY: "auto" }}>
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
                    {r.sms_state !== "off" && (
                      <span className="label-xs" style={{
                        display: "block",
                        color: r.sms_state === "sent" ? "var(--status-success-text)"
                          : r.sms_state === "error" ? "var(--status-error-text)"
                          : "var(--text-tertiary)",
                      }}>
                        SMS: {r.sms_state.replace(/_/g, " ")}
                      </span>
                    )}
                  </td>
                  <td style={{ padding: "var(--space-2) var(--space-3)" }}>
                    {["PENDING", "READY", "REVIEW", ""].includes(r.status) ? (
                      // One control: a pill-styled select that saves on change.
                      <form method="POST" action={`/api/recipients/${r.id}`} className="flex items-center gap-1">
                        <input type="hidden" name="action" value="status" />
                        <select name="status" data-autosubmit defaultValue={r.status || "PENDING"}
                          style={{
                            height: 26, fontSize: "var(--label-xs)", fontWeight: 600,
                            padding: "0 var(--space-2)", borderRadius: 999,
                            border: "1px solid transparent", cursor: "pointer",
                            background: (STATUS_COLORS[r.status || "PENDING"] ?? STATUS_COLORS.PENDING).bg,
                            color: (STATUS_COLORS[r.status || "PENDING"] ?? STATUS_COLORS.PENDING).text,
                          }}>
                          <option value="PENDING">PENDING</option>
                          <option value="READY">READY</option>
                          <option value="REVIEW">REVIEW</option>
                        </select>
                        <noscript>
                          <button type="submit" className="btn btn-tertiary btn-sm" style={{ height: 26, padding: "0 8px" }}>Set</button>
                        </noscript>
                      </form>
                    ) : (
                      <StatusChip status={r.status} />
                    )}
                  </td>
                  <td className="body-xs" style={{ padding: "var(--space-2) var(--space-3)", color: r.email_state === "error" ? "var(--status-error-text)" : "var(--text-tertiary)", maxWidth: 200 }}>{r.notes}</td>
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
      </p>
    </SectionCard>
  );
}

function ConfigureSection({ c, cfg, cards, disclaimers }: {
  c: CampaignRow; cfg: CampaignConfig; cards: AssetRow[]; disclaimers: AssetRow[];
}) {
  const activeCards = cards.filter((a) => a.active);
  const activeDisclaimers = disclaimers.filter((a) => a.active);
  const subPanel = {
    border: "1px solid var(--border-secondary)", borderRadius: "var(--radius-sm)",
    padding: "var(--space-3) var(--space-4)", marginBottom: "var(--space-4)",
    background: "var(--bg-secondary)",
  } as const;
  return (
    <SectionCard icon="pencil" title="2 · Configure">
      {/* ── Email templates: whole-email starting points ── */}
      <div style={subPanel}>
        <div className="flex items-baseline gap-2 flex-wrap" style={{ marginBottom: "var(--space-1)" }}>
          <span className="label-sm" style={{ color: "var(--landing-blue)" }}>Email templates</span>
        </div>
        <p className="body-xs" style={{ color: "var(--text-secondary)", margin: "0 0 var(--space-3)" }}>
          A template rewrites the <strong>entire email</strong> — subject, banner, card,
          body copy, and closing — as a ready-made starting point. Load one, then fill in
          the blanks (manager, dates) and tweak below.
        </p>
        <div className="flex gap-2 flex-wrap">
          {Object.keys(EMAIL_TEMPLATES).map((name) => (
            <form key={name} method="POST" action={`/api/campaigns/${c.id}/template`}>
              <input type="hidden" name="name" value={name} />
              <button type="submit" className="btn btn-secondary btn-sm">{name}</button>
            </form>
          ))}
        </div>
      </div>

      {/* ── Notification card: one swappable block inside the email ── */}
      <div style={subPanel}>
        <div className="flex items-baseline gap-2 flex-wrap" style={{ marginBottom: "var(--space-1)" }}>
          <span className="label-sm" style={{ color: "var(--landing-blue)" }}>Notification card</span>
          <a href="/edit" className="label-xs" style={{ color: "var(--landing-bright-blue)", textDecoration: "none" }}>
            ✎ Manage cards &amp; disclaimers
          </a>
        </div>
        <p className="body-xs" style={{ color: "var(--text-secondary)", margin: "0 0 var(--space-3)" }}>
          Unlike a template, a card is just <strong>one highlight block</strong> dropped into
          the middle of your email. Swap or remove it without touching the rest — applies
          instantly, check the preview below.
        </p>
        <div className="flex gap-2 flex-wrap">
          <form method="POST" action={`/api/campaigns/${c.id}/config-card`}>
            <input type="hidden" name="card" value="" />
            <button type="submit" className={`btn btn-sm ${cfg.notification_card === "" ? "btn-primary" : "btn-tertiary"}`}>
              None
            </button>
          </form>
          {activeCards.map((a) => (
            <form key={a.id} method="POST" action={`/api/campaigns/${c.id}/config-card`}>
              <input type="hidden" name="card" value={String(a.config.key)} />
              <button type="submit"
                className={`btn btn-sm ${cfg.notification_card === String(a.config.key) ? "btn-primary" : "btn-tertiary"}`}>
                {a.config.label || a.config.key}
              </button>
            </form>
          ))}
        </div>
        <div className="flex gap-2 flex-wrap" style={{ marginTop: "var(--space-3)" }}>
          <span className="label-xs" style={{ color: "var(--text-secondary)", alignSelf: "center" }}>
            Disclaimer presets:
          </span>
          {activeDisclaimers.map((a) => (
            <form key={a.id} method="POST" action={`/api/campaigns/${c.id}/config-disclaimer`}>
              <input type="hidden" name="template_id" value={a.id} />
              <button type="submit"
                className={`btn btn-sm ${cfg.disclaimer_html === String(a.config.html) ? "btn-primary" : "btn-tertiary"}`}>
                {a.name}
              </button>
            </form>
          ))}
        </div>
      </div>

      <form method="POST" action={`/api/campaigns/${c.id}/config`} className="flex flex-col gap-3">
        <div className="flex gap-3 flex-wrap">
          <Field label="Event name" grow={2}>
            <input type="text" name="event_name" defaultValue={c.event_name} className="ds-input" style={inputStyle} />
          </Field>
          <Field label="Window start">
            <input type="date" name="window_start" defaultValue={cfg.window_start} className="ds-input" style={inputStyle} />
          </Field>
          <Field label="Window end">
            <input type="date" name="window_end" defaultValue={cfg.window_end} className="ds-input" style={inputStyle} />
          </Field>
        </div>
        <Field label="Subject template" grow={1}>
          <input type="text" name="subject_template" defaultValue={cfg.subject_template} className="ds-input" style={inputStyle} />
        </Field>
        <div className="flex gap-3 flex-wrap">
          <Field label="Sender display name">
            <input type="text" name="sender_display_name" defaultValue={cfg.sender_display_name} className="ds-input" style={inputStyle} />
          </Field>
          <Field label="Reply-to">
            <input type="text" name="reply_to" defaultValue={cfg.reply_to} className="ds-input" style={inputStyle} />
          </Field>
        </div>
        <div className="flex gap-3 flex-wrap">
          <Field label="Manager email (CC'd + {{manager_email}})">
            <input type="text" name="manager_email" defaultValue={cfg.manager_email} className="ds-input" style={inputStyle} />
          </Field>
          <Field label="Manager name ({{manager_name}})">
            <input type="text" name="manager_name" defaultValue={cfg.manager_name} className="ds-input" style={inputStyle} />
          </Field>
          <Field label="Extra CC (comma-separated)">
            <input type="text" name="cc_extra" defaultValue={cfg.cc_extra} className="ds-input" style={inputStyle} />
          </Field>
        </div>
        <div className="flex gap-4 flex-wrap body-sm" style={{ color: "var(--text-secondary)" }}>
          <label className="flex items-center gap-2">
            <input type="checkbox" name="include_disclaimer" defaultChecked={cfg.include_disclaimer} /> Include disclaimer
          </label>
          <label className="flex items-center gap-2">
            <input type="checkbox" name="include_unit_line" defaultChecked={cfg.include_unit_line} /> Include unit line
          </label>
        </div>
        <Field label="Disclaimer HTML (blank = default)">
          <textarea name="disclaimer_html" rows={2} defaultValue={cfg.disclaimer_html} className="ds-input font-mono" style={{ fontSize: "var(--label-xs)" }} />
        </Field>
        <Field label="Greeting HTML">
          <textarea name="greeting_template" rows={2} defaultValue={cfg.greeting_template} className="ds-input font-mono" style={{ fontSize: "var(--label-xs)" }} />
        </Field>
        <Field label="Body intro HTML">
          <textarea name="body_intro_html" rows={5} defaultValue={cfg.body_intro_html} className="ds-input font-mono" style={{ fontSize: "var(--label-xs)" }} />
        </Field>
        <Field label="Closing HTML">
          <textarea name="closing_html" rows={3} defaultValue={cfg.closing_html} className="ds-input font-mono" style={{ fontSize: "var(--label-xs)" }} />
        </Field>
        <Field label="Signature HTML">
          <textarea name="signature_html" rows={2} defaultValue={cfg.signature_html} className="ds-input font-mono" style={{ fontSize: "var(--label-xs)" }} />
        </Field>
        <div className="flex gap-3 flex-wrap">
          <Field label="Attachment Drive IDs (or one folder ID)" grow={2}>
            <input type="text" name="attachment_file_ids" defaultValue={cfg.attachment_file_ids} className="ds-input" style={inputStyle} />
          </Field>
          <Field label="Email background color">
            <input type="text" name="email_background_color" defaultValue={cfg.email_background_color} placeholder="#F5F1E8" className="ds-input" style={inputStyle} />
          </Field>
          <Field label="Header image URL">
            <input type="text" name="email_header_image_url" defaultValue={cfg.email_header_image_url} className="ds-input" style={inputStyle} />
          </Field>
        </div>
        <div className="flex gap-3 flex-wrap items-end">
          <Field label="Dry-run draft limit">
            <input type="number" name="dry_run_limit" defaultValue={cfg.dry_run_limit} min={1} max={50} className="ds-input" style={inputStyle} />
          </Field>
          <Field label="Max recipients per send">
            <input type="number" name="max_per_run" defaultValue={cfg.max_per_run} min={1} max={1500} className="ds-input" style={inputStyle} />
          </Field>
          <span className="flex-1" />
          <button type="submit" className="btn btn-primary btn-sm">Save configuration</button>
        </div>
        <p className="body-xs" style={{ margin: 0, color: "var(--text-tertiary)" }}>
          Tokens: {"{{property_name}} {{event_name}} {{date_range}} {{today}} {{first_name}} {{member_name}} {{member_email}} {{unit}} {{manager_name}} {{manager_email}}"} — values are HTML-escaped automatically.
        </p>
      </form>
    </SectionCard>
  );
}

function PreviewSection({ c, recipients, previewHtml, previewSubject, previewFor, previewRecipientId }: {
  c: CampaignRow; recipients: RecipientRow[];
  previewHtml?: string; previewSubject?: string; previewFor?: string; previewRecipientId?: string;
}) {
  return (
    <SectionCard icon="search" title="3 · Preview">
      <form method="GET" action={`/c/${c.id}`} className="flex items-center gap-2 flex-wrap" style={{ marginBottom: "var(--space-3)" }}>
        <span className="body-xs" style={{ color: "var(--text-tertiary)" }}>Previewing for: <strong>{previewFor}</strong></span>
        <span className="flex-1" />
        <select name="preview" defaultValue={previewRecipientId ?? ""} className="ds-input" style={{ height: 32, fontSize: "var(--label-xs)", maxWidth: 280 }}>
          {recipients.map((r) => (
            <option key={r.id} value={r.id}>{r.unit ? `${r.unit} · ` : ""}{r.email}</option>
          ))}
        </select>
        <button type="submit" className="btn btn-secondary btn-sm">Preview as</button>
      </form>
      <p className="body-sm" style={{ margin: "0 0 var(--space-2)", color: "var(--text-primary)" }}>
        <strong>Subject:</strong> {previewSubject}
      </p>
      <iframe
        srcDoc={previewHtml ?? ""}
        sandbox=""
        style={{ width: "100%", height: 460, border: "1px solid var(--border-secondary)", borderRadius: "var(--radius-sm)", background: "#fff" }}
        title="Email preview"
      />
    </SectionCard>
  );
}

function SendSection({ c, recipients, runs, cfg, userEmail }: {
  c: CampaignRow; recipients: RecipientRow[]; runs: RunRow[]; cfg: CampaignConfig; userEmail: string;
}) {
  const eligible = recipients.filter((r) => ["", "PENDING", "READY"].includes(r.status));
  const sendCount = Math.min(eligible.length, cfg.max_per_run);
  const draftCount = Math.min(eligible.length, cfg.dry_run_limit);
  const canUndo = runs.some((r) => r.kind === "send" && r.completed_at);
  const busy = c.status === "sending" || c.status === "fetching";
  return (
    <SectionCard icon="arrow-right" title="4 · Send">
      {c.status === "sending" && (
        <Banner kind="success">Send in progress — refresh for per-recipient results.</Banner>
      )}
      <div className="flex gap-2 flex-wrap items-center" style={{ marginBottom: "var(--space-4)" }}>
        <form method="POST" action={`/api/campaigns/${c.id}/dispatch`}>
          <input type="hidden" name="kind" value="dryrun" />
          <button type="submit" className="btn btn-secondary btn-sm" disabled={busy || eligible.length === 0}>
            Create {draftCount} draft{draftCount === 1 ? "" : "s"} (dry run)
          </button>
        </form>
        <form method="POST" action={`/api/campaigns/${c.id}/dispatch`}>
          <input type="hidden" name="kind" value="test" />
          <button type="submit" className="btn btn-secondary btn-sm" disabled={busy}>
            Test send to me
          </button>
        </form>
        {canUndo && (
          <form method="POST" action={`/api/campaigns/${c.id}/undo`}>
            <button type="submit" className="btn btn-tertiary btn-sm" disabled={busy}>
              Undo last send
            </button>
          </form>
        )}
      </div>
      <form method="POST" action={`/api/campaigns/${c.id}/dispatch`} className="flex items-center gap-3 flex-wrap"
        style={{ border: "1px solid var(--border-primary)", borderRadius: "var(--radius-sm)", padding: "var(--space-3) var(--space-4)" }}>
        <input type="hidden" name="kind" value="send" />
        <label className="body-sm flex items-center gap-2" style={{ color: "var(--text-primary)" }}>
          <input type="checkbox" name="confirm" />
          Send to <strong>{sendCount}</strong> recipient{sendCount === 1 ? "" : "s"} from member.support@hellolanding.com
        </label>
        <span className="flex-1" />
        <button type="submit" className="btn btn-primary btn-sm" disabled={busy || eligible.length === 0}>
          Send campaign
        </button>
      </form>
      <p className="body-xs" style={{ color: "var(--text-tertiary)", margin: "var(--space-2) 0 0" }}>
        Drafts land in the member.support@ mailbox with subject "[DRAFT] …". Test emails go to {userEmail} with "TEST — …".
        Undo reverts recipient statuses only — emails already sent are not recalled.
      </p>

      {runs.length > 0 && (
        <div style={{ marginTop: "var(--space-4)" }}>
          <h3 className="label-xs" style={{ color: "var(--text-secondary)", textTransform: "uppercase", margin: "0 0 var(--space-2)" }}>Run history</h3>
          <ul style={{ listStyle: "none", margin: 0, padding: 0, border: "1px solid var(--border-secondary)", borderRadius: "var(--radius-sm)" }}>
            {runs.map((r, i) => (
              <li key={r.id} className="flex items-center gap-3 body-xs"
                style={{ padding: "var(--space-2) var(--space-3)", borderTop: i === 0 ? "none" : "1px solid var(--border-secondary)", color: "var(--text-secondary)" }}>
                <span className="label-xs" style={{ minWidth: 52, color: "var(--text-primary)", textTransform: "uppercase" }}>{r.kind}</span>
                <span>{r.started_at.slice(0, 16).replace("T", " ")}</span>
                <span>{r.actor.split("@")[0]}</span>
                <span className="flex-1" />
                {r.error
                  ? <span style={{ color: "var(--status-error-text)" }}>{r.error}</span>
                  : r.completed_at
                    ? <span style={{ color: "var(--status-success-text)" }}>{r.count} ok</span>
                    : <span style={{ color: "var(--status-warning-text)" }}>running…</span>}
              </li>
            ))}
          </ul>
        </div>
      )}
    </SectionCard>
  );
}

function EditListPage({ cards, disclaimers, flash, error }: AppProps) {
  const list = (items: AssetRow[], kind: string, columns: (a: AssetRow) => string) => (
    items.length === 0
      ? <p className="body-sm" style={{ color: "var(--text-tertiary)", margin: 0 }}>None yet.</p>
      : <ul style={{ listStyle: "none", margin: 0, padding: 0, border: "1px solid var(--border-secondary)", borderRadius: "var(--radius-sm)" }}>
          {items.map((a, i) => (
            <li key={a.id} style={{ borderTop: i === 0 ? "none" : "1px solid var(--border-secondary)" }}>
              <a href={`/edit/${kind}/${a.id}`} className="flex items-center gap-3"
                style={{ padding: "var(--space-2) var(--space-4)", textDecoration: "none" }}>
                <span className="body-sm flex-1" style={{ color: a.active ? "var(--text-primary)" : "var(--text-tertiary)" }}>
                  {columns(a)}
                </span>
                {!a.active && <span className="label-xs" style={{ color: "var(--text-tertiary)" }}>inactive</span>}
                <span className="body-xs" style={{ color: "var(--text-tertiary)" }}>
                  {a.updated_by === "seed" ? "built-in" : `${(a.updated_by ?? "").split("@")[0]} · ${(a.updated_at ?? "").slice(0, 10)}`}
                </span>
              </a>
            </li>
          ))}
        </ul>
  );
  return (
    <main className="ds-container flex-1">
      <div className="ds-content w-full max-w-2xl mx-auto flex flex-col gap-6"
        style={{ paddingTop: "var(--space-7)", paddingBottom: "var(--space-9)" }}>
        <div className="flex items-center gap-3">
          <a href="/" className="label-sm flex items-center gap-1" style={{ color: "var(--landing-bright-blue)", textDecoration: "none" }}>
            <Icon name="arrow-short-left" size="sm" /> Campaigns
          </a>
          <span className="flex-1" />
        </div>
        <h1 className="header-lg" style={{ margin: 0 }}>Cards &amp; disclaimers</h1>
        {flash && <Banner kind="success">{flash}</Banner>}
        {error && <Banner kind="error">{error}</Banner>}

        <SectionCard icon="pencil" title="Notification cards">
          <div style={{ marginBottom: "var(--space-3)" }}>
            <a href="/edit/card/new" className="btn btn-primary btn-sm" style={{ textDecoration: "none" }}>+ New card</a>
          </div>
          {list(cards ?? [], "card", (a) => `${a.config.label || a.config.key} — ${a.config.title}`)}
          <p className="body-xs" style={{ color: "var(--text-tertiary)", margin: "var(--space-3) 0 0" }}>
            Active cards appear as chiclets in every campaign's Configure section.
          </p>
        </SectionCard>

        <SectionCard icon="more-info" title="Disclaimer presets">
          <div style={{ marginBottom: "var(--space-3)" }}>
            <a href="/edit/disclaimer/new" className="btn btn-primary btn-sm" style={{ textDecoration: "none" }}>+ New disclaimer</a>
          </div>
          {list(disclaimers ?? [], "disclaimer", (a) => a.name)}
        </SectionCard>
      </div>
    </main>
  );
}

function EditorPage({ editorKind, editing, editorPreviewHtml, flash, error }: AppProps) {
  const isCard = editorKind === "card";
  const cfg = editing?.config ?? {};
  return (
    <main className="ds-container flex-1">
      <div className="ds-content w-full max-w-2xl mx-auto flex flex-col gap-5"
        style={{ paddingTop: "var(--space-6)", paddingBottom: "var(--space-9)" }}>
        <div className="flex items-center gap-3">
          <a href="/edit" className="label-sm flex items-center gap-1" style={{ color: "var(--landing-bright-blue)", textDecoration: "none" }}>
            <Icon name="arrow-short-left" size="sm" /> Cards &amp; disclaimers
          </a>
        </div>
        <h1 className="header-lg" style={{ margin: 0 }}>
          {editing ? `Edit ${isCard ? "card" : "disclaimer"}` : `New ${isCard ? "card" : "disclaimer"}`}
        </h1>
        {flash && <Banner kind="success">{flash}</Banner>}
        {error && <Banner kind="error">{error}</Banner>}

        <SectionCard icon="pencil" title={isCard ? "Card definition" : "Disclaimer definition"}>
          <form method="POST" action={`/api/edit/${editorKind}`} className="flex flex-col gap-3">
            {editing && <input type="hidden" name="id" value={editing.id} />}
            {isCard ? (
              <>
                <div className="flex gap-3 flex-wrap">
                  <Field label="Key (SCREAMING_SNAKE, stored in campaign config)">
                    <input type="text" name="key" required defaultValue={cfg.key ?? ""} placeholder="POOL_CLOSURE" className="ds-input" style={inputStyle} />
                  </Field>
                  <Field label="Chiclet label">
                    <input type="text" name="label" defaultValue={cfg.label ?? ""} placeholder="🏊 Pool Closure" className="ds-input" style={inputStyle} />
                  </Field>
                </div>
                <div className="flex gap-3 flex-wrap">
                  <Field label="Card title (header bar)">
                    <input type="text" name="title" defaultValue={cfg.title ?? ""} className="ds-input" style={inputStyle} />
                  </Field>
                  <Field label="Accent color">
                    <input type="text" name="accent" defaultValue={cfg.accent ?? "#1A61D9"} className="ds-input" style={inputStyle} />
                  </Field>
                  <Field label="Icon (type or paste an emoji — stored Gmail-safe automatically)">
                    <input type="text" name="icon" defaultValue={entitiesToEmoji(cfg.icon ?? "")} className="ds-input" style={inputStyle} />
                  </Field>
                </div>
                <Field label="Body HTML ({{tokens}} supported; inline styles only — Gmail strips <style> blocks)">
                  <textarea name="body_html" rows={10} defaultValue={cfg.body_html ?? ""} className="ds-input font-mono" style={{ fontSize: "var(--label-xs)" }} />
                </Field>
              </>
            ) : (
              <>
                <Field label="Name (chiclet label)">
                  <input type="text" name="name" required defaultValue={cfg.name ?? ""} className="ds-input" style={inputStyle} />
                </Field>
                <Field label="Disclaimer HTML ({{tokens}} supported)">
                  <textarea name="html" rows={6} defaultValue={cfg.html ?? ""} className="ds-input font-mono" style={{ fontSize: "var(--label-xs)" }} />
                </Field>
              </>
            )}
            <div className="flex items-center gap-4">
              <label className="body-sm flex items-center gap-2" style={{ color: "var(--text-secondary)" }}>
                <input type="checkbox" name="active" defaultChecked={editing ? editing.active : true} />
                Active (shown as a chiclet in Configure)
              </label>
              <span className="flex-1" />
              <button type="submit" className="btn btn-primary btn-sm">Save</button>
            </div>
          </form>
        </SectionCard>

        {editing && (
          <SectionCard icon="search" title="Preview (sample tokens)">
            <iframe
              srcDoc={editorPreviewHtml ?? ""}
              sandbox=""
              style={{ width: "100%", height: 320, border: "1px solid var(--border-secondary)", borderRadius: "var(--radius-sm)", background: "#fff" }}
              title="Asset preview"
            />
            <p className="body-xs" style={{ color: "var(--text-tertiary)", margin: "var(--space-2) 0 0" }}>
              Rendered with sample data (Woodhill / Jordan Sample / unit 101). Save, then refresh to update.
            </p>
          </SectionCard>
        )}
      </div>
    </main>
  );
}

function SmsSection({ c, recipients }: { c: CampaignRow; recipients: RecipientRow[] }) {
  const withPhone = recipients.filter((r) => r.phone_e164).length;
  const counts: Record<string, number> = {};
  for (const r of recipients) counts[r.sms_state] = (counts[r.sms_state] ?? 0) + 1;
  const pendingRetry = recipients.filter((r) =>
    r.status === "SENT" && r.phone_e164 && !["sent", "skipped_optout"].includes(r.sms_state)).length;
  const enabled = c.sms_enabled === 1;
  const stateSummary = Object.entries(counts)
    .filter(([k]) => k !== "off")
    .map(([k, v]) => `${v} ${k.replace(/_/g, " ")}`)
    .join(" · ");

  return (
    <SectionCard icon="phone" title="5 · SMS companion (Dialpad)">
      <div className="flex items-center gap-3 flex-wrap" style={{ marginBottom: "var(--space-3)" }}>
        <form method="POST" action={`/api/campaigns/${c.id}/sms-toggle`} className="flex items-center gap-2">
          <label className="body-sm flex items-center gap-2" style={{ color: "var(--text-primary)" }}>
            <input type="checkbox" name="sms_enabled" data-autosubmit defaultChecked={enabled} />
            Text each member an AI summary after their email sends
          </label>
          <noscript><button type="submit" className="btn btn-tertiary btn-sm">Save</button></noscript>
        </form>
        <span className="flex-1" />
        <span className="body-xs" style={{ color: "var(--text-tertiary)" }}>
          {withPhone} of {recipients.length} recipients have an SMS-ready phone
        </span>
      </div>

      {/* Preview of the AI summary */}
      <div style={{
        border: "1px solid var(--border-secondary)", borderRadius: "var(--radius-sm)",
        padding: "var(--space-3) var(--space-4)", marginBottom: "var(--space-3)",
        background: "var(--bg-secondary)",
      }}>
        <div className="flex items-center gap-2 flex-wrap" style={{ marginBottom: "var(--space-2)" }}>
          <span className="label-xs" style={{ color: "var(--text-secondary)" }}>
            SMS text (from +1 415 980-4986)
          </span>
          {c.sms_preview_truncated === 1 && (
            <span className="label-xs" style={{ color: "var(--status-warning-text)" }}>
              hard-truncated to fit — consider regenerating
            </span>
          )}
          <span className="flex-1" />
          <form method="POST" action={`/api/campaigns/${c.id}/dispatch`}>
            <input type="hidden" name="kind" value="sms_preview" />
            <button type="submit" className="btn btn-secondary btn-sm">
              {c.sms_preview_text ? "Regenerate preview" : "Generate preview"}
            </button>
          </form>
        </div>
        {c.sms_preview_text ? (
          <p className="body-sm" style={{
            margin: 0, color: "var(--text-primary)",
            fontFamily: "monospace", whiteSpace: "pre-wrap",
          }}>
            {c.sms_preview_text}
            <span className="body-xs" style={{ color: "var(--text-tertiary)" }}> ({c.sms_preview_text.length} chars)</span>
          </p>
        ) : (
          <p className="body-sm" style={{ margin: 0, color: "var(--text-tertiary)" }}>
            No preview yet — generate one to see exactly what members would receive.
            The same summary is regenerated fresh at send time.
          </p>
        )}
      </div>

      <div className="flex items-center gap-2 flex-wrap">
        <form method="POST" action={`/api/campaigns/${c.id}/dispatch`} className="flex items-center gap-2">
          <input type="hidden" name="kind" value="sms_test" />
          <input type="tel" name="test_number" placeholder="+15551234567" className="ds-input"
            style={{ height: 32, width: 160, fontSize: "var(--label-xs)" }} />
          <button type="submit" className="btn btn-secondary btn-sm">Test SMS to this number</button>
        </form>
        <span className="flex-1" />
        <form method="POST" action={`/api/campaigns/${c.id}/dispatch`}>
          <input type="hidden" name="kind" value="sms_only" />
          <button type="submit" className="btn btn-primary btn-sm" disabled={pendingRetry === 0}>
            Send SMS to {pendingRetry} emailed recipient{pendingRetry === 1 ? "" : "s"}
          </button>
        </form>
      </div>

      {stateSummary && (
        <p className="body-xs" style={{ color: "var(--text-secondary)", margin: "var(--space-3) 0 0" }}>
          SMS states: {stateSummary}
        </p>
      )}
      <p className="body-xs" style={{ color: "var(--text-tertiary)", margin: "var(--space-2) 0 0" }}>
        Quiet hours: texts only go out 08:00–21:00 in each member's local time (by market
        segment); anyone outside the window is marked "skipped quiet hours" — re-send later
        with the button above. Members who opted out of texts are always skipped.
      </p>
    </SectionCard>
  );
}

function CampaignPage(props: AppProps) {
  const { campaigns, recipients, runs, config, flash, error, user } = props;
  const c = campaigns[0];
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
        {c.status === "errored" && stats?.error ? <Banner kind="error">Last operation failed: {String(stats.error)}</Banner> : null}

        <RecipientsSection c={c} recipients={recipients} />
        {config && <ConfigureSection c={c} cfg={config} cards={props.cards ?? []} disclaimers={props.disclaimers ?? []} />}
        {config && <PreviewSection c={c} recipients={recipients}
          previewHtml={props.previewHtml} previewSubject={props.previewSubject}
          previewFor={props.previewFor} previewRecipientId={props.previewRecipientId} />}
        {config && <SendSection c={c} recipients={recipients} runs={runs} cfg={config} userEmail={user.email} />}
        {config && <SmsSection c={c} recipients={recipients} />}
      </div>
    </main>
  );
}

export default function App(props: AppProps) {
  const active = props.page === "edit" || props.page === "editor" ? "edit" : "campaigns";
  return (
    // #E7EFFB — the OG GAS WebApp page background (Landing light blue).
    <div className="min-h-screen flex flex-col" style={{ background: "#E7EFFB" }}>
      <Navbar userEmail={props.role ? props.user.email : undefined} active={active} />
      {props.page === "access" ? <AccessPage user={props.user} role={props.role} />
        : props.page === "campaign" ? <CampaignPage {...props} />
        : props.page === "edit" ? <EditListPage {...props} />
        : props.page === "editor" ? <EditorPage {...props} />
        : <HomePage {...props} />}
      <Footer />
    </div>
  );
}

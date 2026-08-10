// emailkit — token engine, body composer, notification cards, seeded templates.
// Ported from the legacy GAS app (Tokenizer.gs / Cards.gs / Templates.gs) with
// one deliberate change: token VALUES are HTML-escaped by default (legacy
// injected them raw — audit defect 5). Raw-HTML fragment tokens use the
// {{html:name}} prefix.
//
// The per-recipient token substitution is duplicated in
// workflows/mass-notify-dispatch.js (renderTokens) — keep the two in sync.

export const LANDING_IVR_PHONE = "+14152311701";
export const LANDING_IVR_PHONE_DISPLAY = "(415) 231-1701";

export const LANDING_COLORS = {
  DARK_NAVY: "#15192D",
  ACCENT_BLUE: "#1A61D9",
  LIGHT_BLUE: "#E7EFFB",
  WHITE: "#FFFFFF",
  AMBER: "#E8A317",
  ORANGE: "#D4600A",
  RED: "#D9534F",
  GREEN: "#28A745",
  TEXT_GRAY: "#4A4A4A",
};
const C = LANDING_COLORS;

// ── Token engine ─────────────────────────────────────────────────────────────

export function escapeHtml(s: unknown): string {
  return String(s)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

// {{key}} / {{key | fallback}} / {{html:key}} (raw fragment, no escaping).
// `escapeValues=false` is for plain-text targets (subject lines).
export function renderTokens(
  template: string,
  tokens: Record<string, string>,
  escapeValues = true
): string {
  if (!template) return "";
  return String(template).replace(
    /\{\{\s*(html:)?([a-zA-Z0-9_]+)\s*(?:\|\s*([^}]+))?\s*\}\}/g,
    (_, htmlPrefix, key, fallback) => {
      const val = tokens[key];
      const out = (val !== undefined && val !== null && String(val).trim() !== "")
        ? String(val)
        : (fallback != null ? String(fallback).trim() : "");
      return (htmlPrefix || !escapeValues) ? out : escapeHtml(out);
    }
  );
}

// Card icons: operators type/paste a plain emoji; we store HTML hex entities
// (Gmail-safe, matches the legacy GAS convention of escaping non-ASCII).
export function emojiToEntities(s: string): string {
  const trimmed = String(s ?? "").trim();
  if (!trimmed) return "";
  if (/^(&#x?[0-9a-fA-F]+;\s*)+$/.test(trimmed)) return trimmed.replace(/\s+/g, "");
  return Array.from(trimmed).map((ch) => {
    const cp = ch.codePointAt(0)!;
    return cp > 127 ? `&#x${cp.toString(16).toUpperCase()};` : ch;
  }).join("");
}

// Inverse — show the emoji back in the editor field.
export function entitiesToEmoji(s: string): string {
  return String(s ?? "")
    .replace(/&#x([0-9a-fA-F]+);/g, (_m, hex) => String.fromCodePoint(parseInt(hex, 16)))
    .replace(/&#(\d+);/g, (_m, dec) => String.fromCodePoint(parseInt(dec, 10)));
}

export function normalizeTelLinks(html: string): string {
  if (!html) return "";
  let out = String(html);
  out = out.replace(/<a([^>]*?)\/>/gi, "<a$1></a>");
  out = out.replace(/href\s*=\s*(['"])\s*tel\s*:\s*([^'"]+)\1/gi, (_m, _q, num) =>
    `href="tel:${String(num).replace(/[^\d+]/g, "")}"`);
  return out;
}

// ── Date formatting (legacy formatDateRange_ / today, via Intl) ──────────────

function fmtDate(d: Date, tz: string, withYear: boolean): string {
  const opts: Intl.DateTimeFormatOptions = withYear
    ? { weekday: "short", month: "short", day: "numeric", year: "numeric", timeZone: tz }
    : { weekday: "short", month: "short", day: "numeric", timeZone: tz };
  return new Intl.DateTimeFormat("en-US", opts).format(d); // "Wed, Aug 6, 2026"
}

export function formatDateRange(startIso: string, endIso: string, tz: string): string {
  if (!startIso || !endIso) return "";
  // Date-only strings anchor at noon UTC so tz shifts can't move the calendar day.
  const s = new Date(startIso.length <= 10 ? `${startIso}T12:00:00Z` : startIso);
  const e = new Date(endIso.length <= 10 ? `${endIso}T12:00:00Z` : endIso);
  if (isNaN(s.getTime()) || isNaN(e.getTime())) return "";
  const sameDay = fmtDate(s, tz, true) === fmtDate(e, tz, true);
  if (sameDay) return fmtDate(s, tz, true);
  const sameYear = s.getUTCFullYear() === e.getUTCFullYear();
  return sameYear
    ? `${fmtDate(s, tz, false)}–${fmtDate(e, tz, true)}`
    : `${fmtDate(s, tz, true)}–${fmtDate(e, tz, true)}`;
}

export function formatToday(tz: string): string {
  return fmtDate(new Date(), tz, true);
}

// ── Campaign config (legacy Config.gs keys + defaults) ───────────────────────

export interface CampaignConfig {
  subject_template: string;
  greeting_template: string;
  include_disclaimer: boolean;
  disclaimer_html: string;
  notification_card: string;
  body_intro_html: string;
  include_unit_line: boolean;
  closing_html: string;
  signature_html: string;
  body_full_html: string;
  sender_display_name: string;
  manager_email: string;
  manager_name: string;
  reply_to: string;
  cc_extra: string;
  window_start: string; // yyyy-mm-dd
  window_end: string;
  timezone: string;
  dry_run_limit: number;
  max_per_run: number;
  email_background_color: string;
  email_header_image_url: string;
  attachment_file_ids: string;
}

export const CONFIG_DEFAULTS: CampaignConfig = {
  subject_template: "Notice: {{event_name}} — {{property_name}} ({{date_range}})",
  greeting_template: "<p>Dear {{first_name | Resident}},</p>",
  include_disclaimer: true,
  disclaimer_html: "",
  notification_card: "",
  body_intro_html: "",
  include_unit_line: true,
  closing_html: "<p>Thank you for your cooperation and understanding.</p>",
  signature_html: "",
  body_full_html: "",
  sender_display_name: "Landing Notifications",
  manager_email: "",
  manager_name: "",
  reply_to: "",
  cc_extra: "",
  window_start: "",
  window_end: "",
  timezone: "America/Mexico_City",
  dry_run_limit: 10,
  max_per_run: 500,
  email_background_color: "",
  email_header_image_url: "",
  attachment_file_ids: "",
};

export function parseConfig(configJson: string | null): CampaignConfig {
  let raw: Record<string, unknown> = {};
  try { raw = configJson ? JSON.parse(configJson) : {}; } catch { /* defaults */ }
  const cfg: CampaignConfig = { ...CONFIG_DEFAULTS };
  for (const key of Object.keys(CONFIG_DEFAULTS) as (keyof CampaignConfig)[]) {
    const v = raw[key];
    if (v === undefined || v === null) continue;
    if (typeof CONFIG_DEFAULTS[key] === "boolean") (cfg as any)[key] = v === true || v === "YES" || v === "true";
    else if (typeof CONFIG_DEFAULTS[key] === "number") (cfg as any)[key] = Number(v) || CONFIG_DEFAULTS[key];
    else (cfg as any)[key] = String(v);
  }
  return cfg;
}

// ── Notification cards (legacy Cards.gs, resident set) ───────────────────────
// Card HTML keeps {{tokens}} intact; substitution happens per recipient.

function cardShell(accent: string, codepoint: string, title: string, bodyHtml: string): string {
  return `
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"
  style="border:2px solid ${accent};border-radius:8px;overflow:hidden;margin:12px 0;font-family:Arial,Helvetica,sans-serif;">
  <tr>
    <td style="background:${accent};padding:10px 16px;color:${C.WHITE};font-size:15px;font-weight:bold;">
      ${codepoint}&nbsp; ${title}
    </td>
  </tr>
  <tr>
    <td style="background:${C.LIGHT_BLUE};padding:14px 16px;">
      ${bodyHtml}
    </td>
  </tr>
</table>`.trim();
}

function cardRow(label: string, value: string, valueColor?: string): string {
  const color = valueColor || C.DARK_NAVY;
  return `
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"
  style="margin-bottom:6px;">
  <tr>
    <td style="font-size:12px;color:${C.TEXT_GRAY};text-transform:uppercase;
               letter-spacing:0.5px;width:38%;vertical-align:top;padding-right:8px;">
      ${label}
    </td>
    <td style="font-size:14px;font-weight:bold;color:${color};vertical-align:top;">
      ${value}
    </td>
  </tr>
</table>`.trim();
}

// A card is DATA (editable in the app's /edit surface, persisted in D1
// templates rows with kind='card'): the standard shell wraps an accent color,
// icon entity, title, and inner body HTML. SEED_CARDS carries the six legacy
// cards and seeds D1 on first run; after that, D1 is the registry of record.
export interface CardDef {
  key: string;      // SCREAMING_SNAKE config value (legacy parity)
  label: string;    // chiclet label shown in Configure
  accent: string;   // shell border/header color
  icon: string;     // HTML hex entity (avoids Gmail emoji-encoding issues)
  title: string;    // shell header text
  body_html: string; // inner content ({{tokens}} allowed)
}

export function renderCard(def: CardDef): string {
  return cardShell(def.accent, def.icon, def.title, def.body_html);
}

export { cardRow }; // exported for editor previews / future card tooling

export const SEED_CARDS: CardDef[] = [
  {
    key: "FIRE_INSPECTION", label: "🔥 Fire Inspection",
    accent: C.ACCENT_BLUE, icon: "&#x1F525;", title: "Annual Fire Inspection",
    body_html: [
      cardRow("Property", "{{property_name}}"),
      cardRow("Dates", "{{date_range}}", C.ACCENT_BLUE),
      cardRow("Action required",
        "Inspectors will need <strong>access to your unit</strong>. " +
        "Please ensure your smoke detectors are unobstructed."),
    ].join(""),
  },
  {
    key: "WATER_OUTAGE", label: "🚿 Water Outage",
    accent: C.AMBER, icon: "&#x1F6BF;", title: "Planned Water Outage",
    body_html: [
      cardRow("Property", "{{property_name}}"),
      cardRow("Outage window", "{{date_range}}", C.AMBER),
      cardRow("What to expect",
        "Water service will be temporarily <strong>unavailable</strong> during this window. " +
        "We apologize for the inconvenience."),
    ].join(""),
  },
  {
    key: "MAINTENANCE", label: "🔧 Maintenance",
    accent: C.DARK_NAVY, icon: "&#x1F527;", title: "Scheduled Maintenance",
    body_html: [
      cardRow("Property", "{{property_name}}"),
      cardRow("Scheduled", "{{date_range}}", C.ACCENT_BLUE),
      cardRow("Details",
        "Our maintenance team will be on-site for <strong>{{event_name}}</strong>. " +
        "Some common areas may be temporarily unavailable."),
    ].join(""),
  },
  {
    key: "WEATHER_ALERT", label: "⛈️ Weather Alert",
    accent: C.RED, icon: "&#x26C8;&#xFE0F;", title: "Weather Advisory",
    body_html: [
      cardRow("Property", "{{property_name}}"),
      cardRow("Period", "{{date_range}}", C.RED),
      cardRow("Advisory",
        "A <strong>weather advisory</strong> has been issued for your area. " +
        "Please take necessary precautions for your safety and secure any outdoor belongings."),
    ].join(""),
  },
  {
    key: "POWER_OUTAGE", label: "⚡ Power Outage",
    accent: C.ORANGE, icon: "&#x26A1;", title: "Power Outage — Active",
    body_html: [
      cardRow("Property", "{{property_name}}"),
      cardRow("Reported", "{{today}}", C.ORANGE),
      cardRow("Status",
        "<strong>Active</strong> — Landing is engaged with the property " +
        "and working toward the fastest possible resolution."),
      cardRow("Immediate steps",
        "Unplug sensitive electronics &nbsp;·&nbsp; " +
        "Keep fridge &amp; freezer closed &nbsp;·&nbsp; " +
        "Use flashlights, not candles"),
    ].join(""),
  },
  {
    key: "WIFI_OUTAGE", label: "📶 WiFi Outage",
    accent: C.AMBER, icon: "&#x1F4F6;", title: "WiFi Service Disruption — Active",
    body_html: [
      cardRow("Property", "{{property_name}}"),
      cardRow("Reported", "{{today}}", C.AMBER),
      cardRow("Status",
        "<strong>Active</strong> — Landing is coordinating with the property " +
        "and the internet service provider on a resolution."),
      cardRow("In the meantime",
        "Mobile data is available as a backup &nbsp;·&nbsp; " +
        "Disable WiFi on your device to switch automatically"),
    ].join(""),
  },
];

export interface DisclaimerDef { name: string; html: string }

export const SEED_DISCLAIMERS: DisclaimerDef[] = [
  {
    name: "Standard resident banner",
    html: '<div style="text-align:center;background-color:#E7EFFB;border:2.5px solid #15192D;border-radius:6px;padding:10px;color:#15192D;font-style:italic;">This is a notification to all active residents at {{property_name}}. Please see the message below:</div>',
  },
  {
    name: "Urgent resident banner",
    html: '<div style="text-align:center;background-color:#E7EFFB;border:2.5px solid #15192D;border-radius:6px;padding:10px;color:#15192D;font-style:italic;">This is an urgent notification to all active residents at {{property_name}}. Please see the message below:</div>',
  },
  {
    name: "Plain default",
    html: "<p><em>This is an automated mass notification to all active residents.</em></p>",
  },
];

// ── Body composer (legacy buildHtmlBody_) ────────────────────────────────────
// Produces the COMPOSED body template with {{tokens}} intact. Per-recipient
// substitution happens in the workflow (or in-app for preview).

const DEFAULT_DISCLAIMER =
  "<p><em>This is an automated mass notification to all active residents.</em></p>";

export function composeBodyTemplate(cfg: CampaignConfig, cards?: Record<string, CardDef>): string {
  const cardMap = cards ?? Object.fromEntries(SEED_CARDS.map((c) => [c.key, c]));
  if (cfg.body_full_html && cfg.body_full_html.trim()) {
    return wrapWithBranding(cfg, normalizeTelLinks(cfg.body_full_html + (cfg.signature_html || "")));
  }
  const parts: string[] = [];
  parts.push(cfg.greeting_template);
  if (cfg.include_disclaimer) parts.push(cfg.disclaimer_html || DEFAULT_DISCLAIMER);
  if (cfg.notification_card && cardMap[cfg.notification_card]) {
    parts.push(renderCard(cardMap[cfg.notification_card]));
  }
  parts.push(cfg.body_intro_html);
  if (cfg.include_unit_line) parts.push("{{html:unit_line}}");
  parts.push(cfg.closing_html);
  const body = parts.filter(Boolean).join("")
    .replace(/<p>/gi, '<p style="margin:0 0 0.8em 0;">');
  return wrapWithBranding(cfg, normalizeTelLinks(body + (cfg.signature_html || "")));
}

export function wrapWithBranding(cfg: CampaignConfig, html: string): string {
  const bg = cfg.email_background_color || "";
  const imgUrl = cfg.email_header_image_url || "";
  if (!bg && !imgUrl) return html;
  const header = imgUrl
    ? `<div style="text-align:center;padding:0 0 16px 0;">` +
        `<img src="${escapeHtml(imgUrl)}" alt="Landing" ` +
        `style="max-width:240px;height:auto;border:0;display:inline-block;">` +
      `</div>`
    : "";
  return [
    `<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:transparent;">`,
      `<tr><td align="center" style="padding:0;">`,
        `<table role="presentation" cellpadding="0" cellspacing="0" border="0" `,
        `style="max-width:640px;width:100%;background-color:${bg || "#FFFFFF"};border-radius:8px;">`,
          `<tr><td style="padding:32px 36px;font-family:Arial,Helvetica,sans-serif;">`,
            header,
            `<div>`, html, `</div>`,
          `</td></tr>`,
        `</table>`,
      `</td></tr>`,
    `</table>`,
  ].join("");
}

// ── Token maps (legacy buildGlobalTokens_ / buildPerRowTokens_) ──────────────

export function buildGlobalTokens(
  propertyName: string, eventName: string, cfg: CampaignConfig
): Record<string, string> {
  return {
    property_name: propertyName || "",
    event_name: eventName || "",
    date_range: formatDateRange(cfg.window_start, cfg.window_end, cfg.timezone),
    today: formatToday(cfg.timezone),
    manager_email: cfg.manager_email || "",
    manager_name: cfg.manager_name || "",
  };
}

// Keep in sync with buildRecipientTokens in workflows/mass-notify-dispatch.js.
export function buildRecipientTokens(
  globals: Record<string, string>,
  r: { email: string; name: string; unit: string },
  includeUnitLine: boolean
): Record<string, string> {
  const firstName = r.name ? String(r.name).trim().split(/\s+/)[0] : "";
  return {
    ...globals,
    member_email: r.email || "",
    member_name: r.name || "",
    first_name: firstName || "Resident",
    unit: r.unit || "",
    unit_line: includeUnitLine && r.unit
      ? `<p style="margin:0 0 0.8em 0;"><strong>Your unit number is:</strong> ${escapeHtml(r.unit)}</p>`
      : "",
  };
}

// ── Seeded templates (legacy Templates.gs, resident set) ─────────────────────
// Move-In Notification ships in P4 with its own mode.

const DISCLAIMER_BANNER =
  '<div style="text-align:center;background-color:#E7EFFB;border:2.5px solid #15192D;border-radius:6px;padding:10px;color:#15192D;font-style:italic;">This is a notification to all active residents at {{property_name}}. Please see the message below:</div>';
const URGENT_BANNER =
  '<div style="text-align:center;background-color:#E7EFFB;border:2.5px solid #15192D;border-radius:6px;padding:10px;color:#15192D;font-style:italic;">This is an urgent notification to all active residents at {{property_name}}. Please see the message below:</div>';
const IVR_LINK = `<a href="tel:${LANDING_IVR_PHONE}">${LANDING_IVR_PHONE_DISPLAY}</a>`;

// Applied on every template load for fields the operator must re-enter.
export const TEMPLATE_BLANK_KEYS: (keyof CampaignConfig)[] = [
  "manager_email", "manager_name", "window_start", "window_end",
  "reply_to", "cc_extra", "attachment_file_ids", "body_full_html",
  "email_background_color", "email_header_image_url",
];

export const EMAIL_TEMPLATES: Record<string, Partial<CampaignConfig> & { event_name?: string }> = {
  "Annual Fire Inspection": {
    event_name: "Annual Fire Inspection",
    subject_template: "Important Notice: Annual Fire Inspection — {{property_name}} ({{date_range}})",
    greeting_template: "<p>Dear {{first_name | Resident}},</p>",
    include_disclaimer: true,
    disclaimer_html: DISCLAIMER_BANNER,
    notification_card: "FIRE_INSPECTION",
    body_intro_html: "<p>Our property will undergo its <strong>annual fire inspection</strong> during the window shown above. Inspectors will require access to all units — please ensure your unit is accessible and your smoke/CO detectors are unobstructed.</p>",
    include_unit_line: true,
    closing_html: `<p>If you have any questions, please contact your General Manager {{manager_name}} or our 24/7 Member Support Line at ${IVR_LINK}.</p><p>Warm regards,<br>The Landing Team</p>`,
  },
  "Water Outage": {
    event_name: "Planned Water Outage",
    subject_template: "Water Outage Notice — {{property_name}} ({{date_range}})",
    greeting_template: "<p>Dear {{first_name | Resident}},</p>",
    include_disclaimer: true,
    disclaimer_html: DISCLAIMER_BANNER,
    notification_card: "WATER_OUTAGE",
    body_intro_html: "<p>We want to give you advance notice of a <strong>planned water outage</strong> at {{property_name}}. Our maintenance team will be working to complete this as quickly as possible and minimize disruption.</p><p>We recommend storing some water beforehand for your convenience.</p>",
    include_unit_line: false,
    closing_html: `<p>We apologize for any inconvenience. For questions, contact your General Manager {{manager_name}} or Member Support at ${IVR_LINK}.</p><p>Warm regards,<br>The Landing Team</p>`,
  },
  "General Maintenance": {
    event_name: "Scheduled Property Maintenance",
    subject_template: "Maintenance Notice — {{property_name}} ({{date_range}})",
    greeting_template: "<p>Dear {{first_name | Resident}},</p>",
    include_disclaimer: true,
    disclaimer_html: DISCLAIMER_BANNER,
    notification_card: "MAINTENANCE",
    body_intro_html: "<p>We will be conducting <strong>scheduled maintenance</strong> at {{property_name}} during the window shown above. Some common areas may be temporarily unavailable, and maintenance staff may be present on the property.</p>",
    include_unit_line: false,
    closing_html: `<p>Thank you for your patience. For questions, reach your General Manager {{manager_name}} or call Member Support at ${IVR_LINK}.</p><p>Warm regards,<br>The Landing Team</p>`,
  },
  "Weather Alert": {
    event_name: "Weather Advisory",
    subject_template: "Weather Advisory — {{property_name}} ({{date_range}})",
    greeting_template: "<p>Dear {{first_name | Resident}},</p>",
    include_disclaimer: true,
    disclaimer_html: DISCLAIMER_BANNER,
    notification_card: "WEATHER_ALERT",
    body_intro_html: "<p>A <strong>weather advisory</strong> has been issued for the area surrounding {{property_name}}. Please take appropriate precautions for your safety, secure any outdoor belongings, and follow guidance from local authorities.</p>",
    include_unit_line: false,
    closing_html: `<p>Your safety is our priority. For urgent property concerns, contact your General Manager {{manager_name}} or our 24/7 Member Support at ${IVR_LINK}.</p><p>Stay safe,<br>The Landing Team</p>`,
  },
  "Power Outage": {
    event_name: "Power Outage",
    subject_template: "Urgent: Power Outage at {{property_name}} — {{today}}",
    greeting_template: "<p>Dear {{first_name | Resident}},</p>",
    include_disclaimer: true,
    disclaimer_html: URGENT_BANNER,
    notification_card: "POWER_OUTAGE",
    body_intro_html:
      "<p>We are reaching out because <strong>{{property_name}}</strong> is currently " +
      "experiencing an <strong>unexpected power outage</strong> affecting your unit. " +
      "We understand how disruptive this is — Landing is actively engaged with the " +
      "property management team and working toward the fastest possible resolution.</p>" +
      "<p>While service is being restored, please take the following precautions:</p>" +
      "<ul>" +
      "<li><strong>Unplug sensitive electronics</strong> (laptops, TVs, gaming consoles) " +
      "to protect them from potential power surges when electricity is restored.</li>" +
      "<li><strong>Keep your refrigerator and freezer doors closed</strong> — a closed " +
      "refrigerator will maintain safe temperatures for approximately 4 hours.</li>" +
      "<li>Use <strong>flashlights instead of candles</strong> for your safety.</li>" +
      "<li>If you rely on <strong>powered medical equipment</strong>, please contact us " +
      "immediately so we can assist you.</li>" +
      "</ul>",
    include_unit_line: false,
    closing_html:
      "<p>For the latest updates or if you need immediate assistance, please reach out to " +
      "your General Manager <strong>{{manager_name}}</strong> directly, or contact our " +
      `<strong>24/7 Member Support</strong> team at ${IVR_LINK} — we are standing by to help.</p>` +
      "<p>We sincerely apologize for the inconvenience and thank you for your patience.<br>" +
      "The Landing Team</p>",
  },
  "WiFi Outage": {
    event_name: "WiFi Service Disruption",
    subject_template: "Service Notice: WiFi Outage at {{property_name}} — {{today}}",
    greeting_template: "<p>Dear {{first_name | Resident}},</p>",
    include_disclaimer: true,
    disclaimer_html: DISCLAIMER_BANNER,
    notification_card: "WIFI_OUTAGE",
    body_intro_html:
      "<p>We are writing to let you know that <strong>{{property_name}}</strong> is " +
      "currently experiencing a <strong>WiFi service disruption</strong> affecting your unit. " +
      "Our team is actively coordinating with the property and the internet service provider " +
      "to identify the cause and restore service as quickly as possible.</p>" +
      "<p>In the meantime, here are a few things that may help:</p>" +
      "<ul>" +
      "<li>Your <strong>mobile data plan</strong> can serve as a reliable backup for " +
      "essential connectivity while service is restored.</li>" +
      "<li>If your device connects automatically to the property WiFi, consider " +
      "<strong>disabling WiFi on your device temporarily</strong> so it falls back to " +
      "mobile data without interruption.</li>" +
      "<li>Once service is restored, <strong>restarting your devices or toggling WiFi " +
      "off and back on</strong> should reconnect you automatically.</li>" +
      "</ul>",
    include_unit_line: false,
    closing_html:
      "<p>For updates on the status of this outage or if you need any assistance, please " +
      "reach out to your General Manager <strong>{{manager_name}}</strong>, or contact our " +
      `<strong>24/7 Member Support</strong> team at ${IVR_LINK}.</p>` +
      "<p>We appreciate your patience and apologize for any inconvenience this causes.<br>" +
      "The Landing Team</p>",
  },
};

export function applyTemplate(cfg: CampaignConfig, templateName: string): { cfg: CampaignConfig; event_name?: string } | null {
  const t = EMAIL_TEMPLATES[templateName];
  if (!t) return null;
  const next = { ...cfg };
  for (const [key, value] of Object.entries(t)) {
    if (key === "event_name") continue;
    (next as any)[key] = value;
  }
  for (const key of TEMPLATE_BLANK_KEYS) {
    if (!(key in t)) (next as any)[key] = typeof CONFIG_DEFAULTS[key] === "boolean" ? CONFIG_DEFAULTS[key] : "";
  }
  return { cfg: next, event_name: t.event_name };
}

// ── Validation (real, blocking — fixes audit defect 4) ───────────────────────

export function validateForDispatch(
  cfg: CampaignConfig, propertyName: string, eligibleCount: number, kind: string,
  cardKeys?: Set<string>
): string[] {
  const known = cardKeys ?? new Set(SEED_CARDS.map((c) => c.key));
  const errors: string[] = [];
  if (!cfg.subject_template.trim()) errors.push("Subject template is empty.");
  const hasBody = cfg.body_full_html.trim() || cfg.greeting_template.trim() ||
    cfg.body_intro_html.trim() || cfg.closing_html.trim() || cfg.notification_card;
  if (!hasBody) errors.push("Email body is empty — set a template, card, or body text.");
  if (!propertyName.trim()) errors.push("Property name is empty.");
  if (kind !== "test" && eligibleCount === 0) errors.push("No eligible recipients (blank, PENDING, or READY).");
  if (cfg.notification_card && !known.has(cfg.notification_card)) {
    errors.push(`Unknown notification card: ${cfg.notification_card}`);
  }
  return errors;
}

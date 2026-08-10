/**
 * mass-notify-dispatch — Sandy Workflow (v0.2: email + SMS companion)
 *
 * Email send pipeline for the mass-notifications app, plus the P3 SMS
 * companion (OpsVP): one AI-summarized version of the email body per
 * campaign, sent to each member via Dialpad SMS from the Member Support
 * line. Reports per-recipient results via callback.
 *
 * ── Required secrets (per-workflow / org) ────────────────────────────────────
 *   MN_DISPATCH_URL      GAS dispatcher /exec URL (member.support@)
 *   MN_DISPATCH_SECRET   shared secret (DISPATCH_SECRET script property)
 *   DIALPAD_API_KEY      Dialpad write key — SMS sends
 *   AI_GATEWAY_TOKEN     org-level global — Cloudflare AI Gateway
 *   LANDING_API_GRAPHQL_KEY  org-level global — opt-out lookup (users)
 *
 * ── Trigger payload ──────────────────────────────────────────────────────────
 *   {
 *     campaign_id, app_run_id, callback_url, callback_token,
 *     kind: "send" | "dryrun" | "test" | "sms_preview" | "sms_test" | "sms_only",
 *     config: {
 *       subjectTemplate, bodyTemplate,        // {{tokens}} intact
 *       senderName, replyTo, cc, includeUnitLine,
 *       globalTokens, configAttachmentIds,
 *     },
 *     recipients: [ { id, email, name, unit, attachmentIds,
 *                     phone_e164, segment_timezone } ],
 *     sms: {                                   // required for send(+enabled)/sms_*
 *       enabled: boolean,
 *       from_number: "+14159804986",
 *       quiet_start: 8, quiet_end: 21,         // local hours [start, end)
 *       test_number: "+1..."                    // sms_test only
 *     }
 *   }
 *
 *   Kind semantics:
 *     send        → emails; then, if sms.enabled, SMS phase to the same targets
 *     dryrun/test → email drafts / operator test (no SMS)
 *     sms_preview → AI summary only; callback carries sms_text (nothing sent)
 *     sms_test    → AI summary sent to sms.test_number only
 *     sms_only    → SMS phase only (post-send retry / standalone)
 *
 * ── Callback body ────────────────────────────────────────────────────────────
 *   { run_id, callback_token, campaign_id, app_run_id, kind,
 *     status: "complete" | "error",
 *     results: [ { id, email, ok, error } ],            // email phase
 *     sms_text: string | null,                          // the summary used
 *     sms_truncated: boolean,                           // hard-truncate flag
 *     sms_results: [ { id, state, error } ],            // sent|error|skipped_*
 *     sms_warning: string | null,                       // e.g. opt-out lookup failed
 *     quota_remaining, error }
 */

import { WorkflowEntrypoint } from "cloudflare:workers";

const BATCH_SIZE = 10; // dispatcher MAX_MESSAGES_PER_CALL
const SMS_MAX_CHARS = 320;
const AI_GATEWAY = "https://gateway.ai.cloudflare.com/v1/d094105625bec434114c0b80ecfa7238/sandy-workflows/compat/chat/completions";
const AI_MODEL = "dynamic/sandy-workflows";
const DIALPAD_SMS_URL = "https://dialpad.com/api/v2/sms";
const LANDING_GRAPHQL_URL = "https://api.hellolanding.com/api/v1/graphql";

// ── Token engine — KEEP IN SYNC with src/emailkit.ts (renderTokens etc.) ─────

function escapeHtml(s) {
  return String(s)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function renderTokens(template, tokens, escapeValues = true) {
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

function buildRecipientTokens(globals, r, includeUnitLine) {
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

// ── Email dispatcher client ──────────────────────────────────────────────────

async function dispatch(mode, messages, secrets) {
  const url = secrets?.MN_DISPATCH_URL;
  const secret = secrets?.MN_DISPATCH_SECRET;
  if (!url || !secret) throw new Error("MN_DISPATCH_URL / MN_DISPATCH_SECRET secret missing");
  const res = await fetch(url, {
    method: "POST",
    redirect: "follow", // GAS 302s /exec to googleusercontent
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ secret, mode, messages }),
    signal: AbortSignal.timeout(300000),
  });
  if (!res.ok) throw new Error(`Dispatcher HTTP ${res.status}: ${(await res.text()).slice(0, 300)}`);
  const body = await res.json();
  if (body.status === "error" && !Array.isArray(body.results)) {
    throw new Error(`Dispatcher error: ${body.message || JSON.stringify(body).slice(0, 300)}`);
  }
  return body;
}

// ── SMS helpers ──────────────────────────────────────────────────────────────

// Rough HTML → plain text for the summarizer input.
function htmlToText(html) {
  return String(html || "")
    .replace(/<style[\s\S]*?<\/style>/gi, " ")
    .replace(/<br\s*\/?>/gi, "\n")
    .replace(/<\/(p|div|tr|li|table)>/gi, "\n")
    .replace(/<[^>]+>/g, " ")
    .replace(/&nbsp;/gi, " ")
    .replace(/&amp;/gi, "&").replace(/&lt;/gi, "<").replace(/&gt;/gi, ">")
    .replace(/&quot;/gi, '"').replace(/&#39;/gi, "'")
    .replace(/&#x([0-9a-fA-F]+);/g, (_m, h) => { try { return String.fromCodePoint(parseInt(h, 16)); } catch { return ""; } })
    .replace(/[ \t]+/g, " ")
    .replace(/\s*\n\s*/g, "\n")
    .trim();
}

async function aiSummarize(subject, bodyText, secrets, retryNote) {
  const token = secrets?.AI_GATEWAY_TOKEN;
  if (!token) throw new Error("AI_GATEWAY_TOKEN secret missing");
  const system =
    "You write SMS notifications for residents of Landing apartment properties. " +
    "Summarize the given email into ONE plain-text SMS of AT MOST 300 characters. " +
    "Rules: include the property name; state the key facts (what, when, what the " +
    "resident should do); no invented facts; no links unless a URL appears in the " +
    "email; no emojis; no greeting or sign-off; do not mention that this is a summary.";
  const user = `Subject: ${subject}\n\nEmail body:\n${bodyText.slice(0, 6000)}` +
    (retryNote ? `\n\nIMPORTANT: ${retryNote}` : "");
  const res = await fetch(AI_GATEWAY, {
    method: "POST",
    // RAW token in cf-aig-authorization (NO "Bearer") — gateway convention.
    headers: { "cf-aig-authorization": token, "content-type": "application/json" },
    body: JSON.stringify({
      model: AI_MODEL,
      max_tokens: 300,
      temperature: 0.2,
      messages: [
        { role: "system", content: system },
        { role: "user", content: user },
      ],
    }),
    signal: AbortSignal.timeout(120000),
  });
  if (!res.ok) throw new Error(`AI Gateway ${res.status}: ${(await res.text()).slice(0, 300)}`);
  const data = await res.json();
  return (data.choices?.[0]?.message?.content ?? "").trim();
}

// Summary with length guard: one strict retry, then sentence-boundary truncate.
async function summarizeForSms(subject, bodyHtml, secrets) {
  const text = htmlToText(bodyHtml);
  let out = await aiSummarize(subject, text, secrets, null);
  let truncated = false;
  if (out.length > SMS_MAX_CHARS) {
    out = await aiSummarize(subject, text, secrets,
      `Your previous answer was ${out.length} characters — too long. Rewrite it under 280 characters.`);
  }
  if (out.length > SMS_MAX_CHARS) {
    const cut = out.slice(0, SMS_MAX_CHARS - 1);
    const lastStop = Math.max(cut.lastIndexOf(". "), cut.lastIndexOf("! "), cut.lastIndexOf("? "));
    out = (lastStop > 80 ? cut.slice(0, lastStop + 1) : cut).trim() + "…";
    truncated = true;
  }
  return { text: out, truncated };
}

// Rails-style tz names (warehouse MS_TIMEZONE) → IANA. Default Central.
const RAILS_TZ = {
  "Eastern Time (US & Canada)": "America/New_York",
  "Central Time (US & Canada)": "America/Chicago",
  "Mountain Time (US & Canada)": "America/Denver",
  "Arizona": "America/Phoenix",
  "Pacific Time (US & Canada)": "America/Los_Angeles",
  "Alaska": "America/Anchorage",
  "Hawaii": "Pacific/Honolulu",
};

function localHour(railsTz) {
  const iana = RAILS_TZ[String(railsTz || "").trim()] || "America/Chicago";
  try {
    return parseInt(new Intl.DateTimeFormat("en-US", {
      hour: "numeric", hour12: false, timeZone: iana,
    }).format(new Date()), 10);
  } catch {
    return 12; // unknown tz — assume mid-day rather than blocking
  }
}

// Opt-out lookup: User.text_notifications_enabled === false → skip.
// Graceful: on any failure, return empty set + a warning (parity: the manual
// Hub-Manager SMS process had no opt-out check at all).
async function fetchOptOuts(emails, secrets) {
  const key = secrets?.LANDING_API_GRAPHQL_KEY;
  const optedOut = new Set();
  if (!key || emails.length === 0) {
    return { optedOut, warning: key ? null : "opt-out lookup skipped: LANDING_API_GRAPHQL_KEY missing" };
  }
  try {
    for (let i = 0; i < emails.length; i += 100) {
      const chunk = emails.slice(i, i + 100);
      const q = `{ users(per_page: 100, filters: [{ field: "email", operator: "in", value: ${JSON.stringify(chunk)} }]) { data { email text_notifications_enabled } } }`;
      const res = await fetch(LANDING_GRAPHQL_URL, {
        method: "POST",
        headers: { "X-API-TOKEN": key, "Content-Type": "application/json" },
        body: JSON.stringify({ query: q }),
        signal: AbortSignal.timeout(60000),
      });
      if (!res.ok) throw new Error(`GraphQL ${res.status}`);
      const data = await res.json();
      if (data.errors) throw new Error(JSON.stringify(data.errors).slice(0, 200));
      for (const u of data.data?.users?.data ?? []) {
        if (u.text_notifications_enabled === false) optedOut.add(String(u.email).toLowerCase());
      }
      await new Promise((r) => setTimeout(r, 1100)); // 60 req/min limit
    }
    return { optedOut, warning: null };
  } catch (err) {
    return { optedOut: new Set(), warning: `opt-out lookup failed (SMS proceeded): ${String(err?.message || err).slice(0, 150)}` };
  }
}

async function dialpadSms(toNumber, text, fromNumber, secrets) {
  const key = secrets?.DIALPAD_API_KEY;
  if (!key) throw new Error("DIALPAD_API_KEY secret missing");
  const res = await fetch(DIALPAD_SMS_URL, {
    method: "POST",
    headers: { "Authorization": `Bearer ${key}`, "Content-Type": "application/json" },
    body: JSON.stringify({ from_number: fromNumber, to_numbers: [toNumber], text }),
    signal: AbortSignal.timeout(60000),
  });
  if (!res.ok) throw new Error(`Dialpad ${res.status}: ${(await res.text()).slice(0, 200)}`);
  return true;
}

// ── Callback helper (standard Sandy pattern — do not modify) ─────────────────

async function sendCallback(callbackUrl, payload, sandySecrets) {
  const secret = sandySecrets?._sandyCallbackSecret;
  const bodyText = JSON.stringify(payload);
  const headers = { "Content-Type": "application/json" };
  if (secret) {
    const pathname = new URL(callbackUrl).pathname;
    const key = await crypto.subtle.importKey(
      "raw", new TextEncoder().encode(secret),
      { name: "HMAC", hash: "SHA-256" }, false, ["sign"]
    );
    const mac = await crypto.subtle.sign(
      "HMAC", key, new TextEncoder().encode(`POST|${pathname}|${bodyText}`)
    );
    headers["X-Sandy-Workflow-Callback"] = Array.from(new Uint8Array(mac))
      .map((b) => b.toString(16).padStart(2, "0")).join("");
  }
  const res = await fetch(callbackUrl, { method: "POST", headers, body: bodyText, signal: AbortSignal.timeout(30000) });
  if (!res.ok) throw new Error(`Callback failed: HTTP ${res.status}`);
  return { delivered: true };
}

// ── Workflow ─────────────────────────────────────────────────────────────────

const KINDS = ["send", "dryrun", "test", "sms_preview", "sms_test", "sms_only", "card_gen"];

// ── Card generation (editor "describe your card" feature) ────────────────────
// Pinned to Haiku for cheap, fast HTML transforms; falls back to the gateway's
// dynamic route if the pinned slug is rejected.

const CARD_GEN_MODEL = "anthropic/claude-haiku-4-5-20251001";

async function aiGenerateCardHtml(title, description, secrets) {
  const token = secrets?.AI_GATEWAY_TOKEN;
  if (!token) throw new Error("AI_GATEWAY_TOKEN secret missing");
  const system =
    "You generate the inner body HTML for a Landing resident notification card " +
    "(an email component). Output ONLY raw HTML — no markdown fences, no commentary. " +
    "Constraints: email-safe HTML with ALL styles inline (Gmail strips <style> blocks). " +
    "Compose 2–4 key/value rows, each following EXACTLY this pattern: " +
    '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:6px;">' +
    '<tr><td style="font-size:12px;color:#4A4A4A;text-transform:uppercase;letter-spacing:0.5px;' +
    'width:38%;vertical-align:top;padding-right:8px;">LABEL</td>' +
    '<td style="font-size:14px;font-weight:bold;color:#15192D;vertical-align:top;">VALUE</td></tr></table> ' +
    "Row order: a Property row first with value {{property_name}}; a timing row with " +
    "{{date_range}} (scheduled windows) or {{today}} (live incidents) when relevant; then " +
    "1–2 guidance rows telling residents what to expect or do. You may use these tokens, " +
    "substituted later: {{property_name}}, {{event_name}}, {{date_range}}, {{today}}, " +
    "{{manager_name}}. Use <strong> for emphasis inside values. Do NOT include the outer " +
    "card shell (border, colored header) — only the inner rows.";
  const call = async (model) => {
    const res = await fetch(AI_GATEWAY, {
      method: "POST",
      headers: { "cf-aig-authorization": token, "content-type": "application/json" },
      body: JSON.stringify({
        model, max_tokens: 1000, temperature: 0.4,
        messages: [
          { role: "system", content: system },
          { role: "user", content: `Card title: ${title}\n\nDescribe the card to generate:\n${description}` },
        ],
      }),
      signal: AbortSignal.timeout(120000),
    });
    if (!res.ok) throw new Error(`AI Gateway ${res.status}: ${(await res.text()).slice(0, 200)}`);
    return ((await res.json()).choices?.[0]?.message?.content ?? "").trim();
  };
  let out;
  try { out = await call(CARD_GEN_MODEL); }
  catch { out = await call(AI_MODEL); } // fallback: dynamic route
  // Strip markdown fences if the model wrapped its output anyway.
  return out.replace(/^```(?:html)?\s*/i, "").replace(/\s*```$/, "").trim();
}

export class TenantWorkflow extends WorkflowEntrypoint {
  async run(event, step) {
    const { campaign_id, app_run_id, kind, config, recipients, sms, card_gen, callback_url, callback_token } = event.payload || {};
    const secrets = event.payload?._sandySecrets || {};

    try {
      await step.do("validate-inputs", async () => {
        if (!callback_url) throw new Error("callback_url is required");
        if (!KINDS.includes(kind)) throw new Error(`bad kind: ${kind}`);
        if (kind === "card_gen") {
          if (!card_gen?.card_id || !card_gen?.description) throw new Error("card_gen.card_id and description required");
          return { ok: true };
        }
        if (!config?.subjectTemplate || !config?.bodyTemplate) throw new Error("config templates missing");
        const isEmailKind = ["send", "dryrun", "test"].includes(kind);
        if (isEmailKind && (!secrets.MN_DISPATCH_URL || !secrets.MN_DISPATCH_SECRET)) {
          throw new Error("dispatcher secrets missing");
        }
        if (isEmailKind || kind === "sms_only") {
          if (!Array.isArray(recipients) || recipients.length === 0) throw new Error("recipients[] is empty");
        }
        if (kind === "sms_test" && !sms?.test_number) throw new Error("sms.test_number is required");
        return { ok: true };
      });

      // ── Card generation (standalone kind — no email/SMS phases) ───────────
      if (kind === "card_gen") {
        const bodyHtml = await step.do(
          "generate-card-html",
          { retries: { limit: 2, delay: "10 seconds", backoff: "exponential" } },
          async () => aiGenerateCardHtml(card_gen.title || "Notification", card_gen.description, secrets)
        );
        await step.do(
          "send-callback",
          { retries: { limit: 2, delay: "30 seconds", backoff: "linear" } },
          async () => sendCallback(callback_url, {
            run_id: event.instanceId,
            callback_token, campaign_id: null, app_run_id: null, kind,
            status: "complete",
            card_id: card_gen.card_id,
            card_body_html: bodyHtml,
            results: [], sms_text: null, sms_truncated: false, sms_results: [], sms_warning: null,
            quota_remaining: null, error: null,
          }, secrets)
        );
        return { ok: true, generated_chars: bodyHtml.length };
      }

      // ── Email phase ────────────────────────────────────────────────────────
      const results = [];
      let quotaRemaining = null;
      if (["send", "dryrun", "test"].includes(kind)) {
        const dispatcherMode = kind === "dryrun" ? "draft" : "send";
        const subjectPrefix = kind === "dryrun" ? "[DRAFT] " : kind === "test" ? "TEST — " : "";
        for (let i = 0; i < recipients.length; i += BATCH_SIZE) {
          const batch = recipients.slice(i, i + BATCH_SIZE);
          const batchResult = await step.do(
            `dispatch-batch-${Math.floor(i / BATCH_SIZE) + 1}`,
            { retries: { limit: 1, delay: "30 seconds", backoff: "linear" } },
            async () => {
              const messages = batch.map((r) => {
                const tokens = buildRecipientTokens(config.globalTokens || {}, r, !!config.includeUnitLine);
                const attachmentFileIds = [
                  ...(config.configAttachmentIds || []),
                  ...(r.attachmentIds || []),
                ];
                return {
                  to: r.email,
                  cc: config.cc || "",
                  replyTo: config.replyTo || "",
                  senderName: config.senderName || "Landing Notifications",
                  subject: subjectPrefix + renderTokens(config.subjectTemplate, tokens, false),
                  htmlBody: renderTokens(config.bodyTemplate, tokens, true),
                  plainText: "Please view this email in an HTML-capable client.",
                  ...(attachmentFileIds.length ? { attachmentFileIds } : {}),
                };
              });
              return dispatch(dispatcherMode, messages, secrets);
            }
          );
          quotaRemaining = batchResult.quotaRemaining ?? quotaRemaining;
          const brs = batchResult.results || [];
          batch.forEach((r, j) => {
            const br = brs[j] || { ok: false, error: "no dispatcher result" };
            results.push({ id: r.id, email: r.email, ok: !!br.ok, error: br.error || null });
          });
        }
      }

      // ── SMS phase ──────────────────────────────────────────────────────────
      let smsText = null;
      let smsTruncated = false;
      let smsWarning = null;
      const smsResults = [];
      const smsWanted =
        kind === "sms_preview" || kind === "sms_test" || kind === "sms_only" ||
        (kind === "send" && sms?.enabled);

      if (smsWanted) {
        const summary = await step.do(
          "sms-summarize",
          { retries: { limit: 2, delay: "15 seconds", backoff: "exponential" } },
          async () => {
            // Generic-recipient render: the summary is per-campaign, not per-member.
            const tokens = buildRecipientTokens(config.globalTokens || {},
              { email: "", name: "Resident", unit: "" }, false);
            const subject = renderTokens(config.subjectTemplate, tokens, false);
            const body = renderTokens(config.bodyTemplate, tokens, true);
            return summarizeForSms(subject, body, secrets);
          }
        );
        smsText = summary.text;
        smsTruncated = summary.truncated;

        if (kind === "sms_test") {
          await step.do("sms-test-send", async () =>
            dialpadSms(sms.test_number, `TEST — ${smsText}`, sms.from_number, secrets));
          smsResults.push({ id: "sms_test", state: "sent", error: null });
        } else if (kind === "sms_only" || (kind === "send" && sms?.enabled)) {
          // For 'send', only recipients whose email actually went out get SMS.
          const okIds = kind === "send" ? new Set(results.filter((r) => r.ok).map((r) => r.id)) : null;
          const targets = recipients.filter((r) => (okIds ? okIds.has(r.id) : true));

          const optOut = await step.do("sms-optout-lookup", async () =>
            fetchOptOuts(targets.map((t) => String(t.email).toLowerCase()), secrets));
          smsWarning = optOut.warning;
          const optedOut = new Set(optOut.optedOut);

          for (let i = 0; i < targets.length; i += BATCH_SIZE) {
            const batch = targets.slice(i, i + BATCH_SIZE);
            const batchOut = await step.do(
              `sms-batch-${Math.floor(i / BATCH_SIZE) + 1}`,
              { retries: { limit: 1, delay: "30 seconds", backoff: "linear" } },
              async () => {
                const out = [];
                for (const r of batch) {
                  if (!r.phone_e164) {
                    out.push({ id: r.id, state: "skipped_no_phone", error: null });
                    continue;
                  }
                  if (optedOut.has(String(r.email).toLowerCase())) {
                    out.push({ id: r.id, state: "skipped_optout", error: null });
                    continue;
                  }
                  const h = localHour(r.segment_timezone);
                  const qs = sms?.quiet_start ?? 8, qe = sms?.quiet_end ?? 21;
                  if (h < qs || h >= qe) {
                    out.push({ id: r.id, state: "skipped_quiet_hours", error: null });
                    continue;
                  }
                  try {
                    await dialpadSms(r.phone_e164, smsText, sms.from_number, secrets);
                    out.push({ id: r.id, state: "sent", error: null });
                  } catch (err) {
                    out.push({ id: r.id, state: "error", error: String(err?.message || err).slice(0, 200) });
                  }
                  await new Promise((res) => setTimeout(res, 150)); // pacing
                }
                return out;
              }
            );
            smsResults.push(...batchOut);
          }
        }
      }

      await step.do(
        "send-callback",
        { retries: { limit: 2, delay: "30 seconds", backoff: "linear" } },
        async () => sendCallback(callback_url, {
          run_id: event.instanceId,
          callback_token, campaign_id, app_run_id, kind,
          status: "complete",
          results,
          sms_text: smsText,
          sms_truncated: smsTruncated,
          sms_results: smsResults,
          sms_warning: smsWarning,
          quota_remaining: quotaRemaining,
          error: null,
        }, secrets)
      );

      return {
        ok: true,
        emails_ok: results.filter((r) => r.ok).length,
        sms_sent: smsResults.filter((r) => r.state === "sent").length,
      };
    } catch (err) {
      if (callback_url) {
        await step.do("send-error-callback", async () => sendCallback(callback_url, {
          run_id: event.instanceId,
          callback_token, campaign_id, app_run_id, kind,
          status: "error",
          results: [], sms_text: null, sms_truncated: false, sms_results: [], sms_warning: null,
          quota_remaining: null,
          error: String(err?.message || err),
        }, secrets));
      }
      throw err;
    }
  }
}

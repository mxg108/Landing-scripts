/**
 * mass-notify-dispatch — Sandy Workflow
 *
 * Email send pipeline for the mass-notifications app. Receives a composed
 * body/subject template (tokens intact) plus per-recipient token data,
 * renders each message, and dispatches through the GAS mail dispatcher
 * deployed under member.support@hellolanding.com (payload mode, batches of
 * up to 10 messages per call). Reports per-recipient results via callback.
 *
 * ── Required secrets (per-workflow, already set) ─────────────────────────────
 *   MN_DISPATCH_URL      GAS dispatcher /exec URL
 *   MN_DISPATCH_SECRET   shared secret (DISPATCH_SECRET script property)
 *
 * ── Trigger payload ──────────────────────────────────────────────────────────
 *   {
 *     campaign_id: string,
 *     app_run_id:  string,             // app D1 runs.id for audit correlation
 *     kind: "send" | "dryrun" | "test",
 *     config: {
 *       subjectTemplate: string,       // {{tokens}} intact
 *       bodyTemplate:    string,       // composed HTML, {{tokens}} intact
 *       senderName:      string,
 *       replyTo:         string,
 *       cc:              string,       // manager_email + cc_extra, comma-joined
 *       includeUnitLine: boolean,
 *       globalTokens:    { property_name, event_name, date_range, today,
 *                          manager_email, manager_name },
 *       configAttachmentIds: string[], // campaign-level Drive IDs
 *     },
 *     recipients: [ { id, email, name, unit, attachmentIds: string[] } ],
 *     callback_url, callback_token
 *   }
 *
 *   kind semantics (legacy parity):
 *     send   → GmailApp send to each recipient
 *     dryrun → Gmail DRAFTS in the member.support@ mailbox, subject "[DRAFT] "
 *     test   → real send of ONE rendered sample to the operator (recipients[0]
 *              carries the operator's email), subject "TEST — "
 *
 * ── Callback body ────────────────────────────────────────────────────────────
 *   { run_id, callback_token, campaign_id, app_run_id, kind,
 *     status: "complete" | "error",
 *     results: [ { id, email, ok, error } ],
 *     quota_remaining: number | null,
 *     error: string | null }
 */

import { WorkflowEntrypoint } from "cloudflare:workers";

const BATCH_SIZE = 10; // dispatcher MAX_MESSAGES_PER_CALL

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

// ── Dispatcher client ────────────────────────────────────────────────────────

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
  return body; // { status, quotaRemaining, results: [{to, ok, error?}] }
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

export class TenantWorkflow extends WorkflowEntrypoint {
  async run(event, step) {
    const { campaign_id, app_run_id, kind, config, recipients, callback_url, callback_token } = event.payload || {};
    const secrets = event.payload?._sandySecrets || {};

    try {
      await step.do("validate-inputs", async () => {
        if (!secrets.MN_DISPATCH_URL || !secrets.MN_DISPATCH_SECRET) {
          throw new Error("dispatcher secrets missing");
        }
        if (!callback_url) throw new Error("callback_url is required");
        if (!config?.subjectTemplate || !config?.bodyTemplate) throw new Error("config templates missing");
        if (!Array.isArray(recipients) || recipients.length === 0) throw new Error("recipients[] is empty");
        if (!["send", "dryrun", "test"].includes(kind)) throw new Error(`bad kind: ${kind}`);
        return { ok: true, count: recipients.length };
      });

      const dispatcherMode = kind === "dryrun" ? "draft" : "send";
      const subjectPrefix = kind === "dryrun" ? "[DRAFT] " : kind === "test" ? "TEST — " : "";

      const results = [];
      let quotaRemaining = null;

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

      await step.do(
        "send-callback",
        { retries: { limit: 2, delay: "30 seconds", backoff: "linear" } },
        async () => sendCallback(callback_url, {
          run_id: event.instanceId,
          callback_token,
          campaign_id,
          app_run_id,
          kind,
          status: "complete",
          results,
          quota_remaining: quotaRemaining,
          error: null,
        }, secrets)
      );

      return { ok: true, sent: results.filter((r) => r.ok).length, failed: results.filter((r) => !r.ok).length };
    } catch (err) {
      if (callback_url) {
        await step.do("send-error-callback", async () => sendCallback(callback_url, {
          run_id: event.instanceId,
          callback_token,
          campaign_id,
          app_run_id,
          kind,
          status: "error",
          results: [],
          quota_remaining: null,
          error: String(err?.message || err),
        }, secrets));
      }
      throw err;
    }
  }
}

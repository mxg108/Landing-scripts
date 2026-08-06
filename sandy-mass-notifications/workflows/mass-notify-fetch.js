/**
 * mass-notify-fetch — Sandy Workflow
 *
 * Fetches the active residents of a property from the Landing warehouse
 * (Snowflake LANDING.CORE dims via Sandy's MCP gateway) and POSTs them
 * back to the mass-notifications app as campaign recipients.
 *
 * Replaces the legacy Looker "Active Occupants" sync (dashboard 4552
 * replica) with the same eligibility semantics — validated 2026-08-05 to
 * return the same members/reservation IDs as the Sigma workbook
 * "Member Information/Emails" (Time Horizon = Current) for the pilot
 * property. See sandy-mass-notifications/PRD.md §6.1.
 *
 * ── Required secrets ─────────────────────────────────────────────────────────
 *   MCPGW_SNOWFLAKE_TOKEN   org-level global secret (already provisioned) —
 *                           machine token for Sandy's Snowflake MCP gateway.
 *
 * ── Trigger payload ──────────────────────────────────────────────────────────
 *   {
 *     property_name:  string,   // exact DIMPROPERTY.PROPERTY_NAME value
 *     campaign_id:    string,   // app D1 campaigns.id — echoed back
 *     callback_url:   string,
 *     callback_token: string
 *   }
 *
 * ── Callback body ────────────────────────────────────────────────────────────
 *   {
 *     run_id, callback_token, campaign_id, property_name,
 *     status: "complete" | "error",
 *     recipients: [ { reservation_id, email, name, unit, phone_e164,
 *                     phone_raw, segment_timezone, market_segment, agm_name,
 *                     status, notes } ],   // status: "PENDING" | "REVIEW"
 *     stats: { raw, eligible, review, dropped_invalid_email },
 *     error: string | null
 *   }
 */

import { WorkflowEntrypoint } from "cloudflare:workers";

const SNOWFLAKE_GATEWAY = "https://sandy.hellolanding.tech/mcp-gateway/snowflake";
const ROW_LIMIT = 1000; // safety ceiling; legacy max_per_run is 500

// ── Snowflake MCP gateway client ─────────────────────────────────────────────
// Streamable-HTTP MCP, machine-to-machine auth via X-MCP-Token (sgmcp_ token).
// The server is stateless; we send initialize + tools/call per invocation.

async function snowflakeSql(sql, secrets) {
  const token = secrets?.MCPGW_SNOWFLAKE_TOKEN;
  if (!token) throw new Error("MCPGW_SNOWFLAKE_TOKEN secret missing");

  const call = async (body) => {
    const res = await fetch(SNOWFLAKE_GATEWAY, {
      method: "POST",
      headers: {
        "X-MCP-Token": token,
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
      },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(120000),
    });
    if (!res.ok) throw new Error(`Snowflake gateway HTTP ${res.status}: ${(await res.text()).slice(0, 300)}`);
    let raw = await res.text();
    // Tolerate SSE framing ("data: {...}") and plain JSON alike.
    for (const line of raw.split("\n")) {
      if (line.startsWith("data:")) { raw = line.slice(5).trim(); break; }
    }
    const rpc = JSON.parse(raw);
    if (rpc.error) throw new Error(`Snowflake gateway RPC error: ${JSON.stringify(rpc.error).slice(0, 300)}`);
    return rpc.result;
  };

  await call({
    jsonrpc: "2.0", id: 0, method: "initialize",
    params: { protocolVersion: "2025-03-26", capabilities: {}, clientInfo: { name: "mass-notify-fetch", version: "1.0" } },
  });
  const result = await call({
    jsonrpc: "2.0", id: 1, method: "tools/call",
    params: { name: "snowflake_sql_exec_tool", arguments: { sql } },
  });

  const text = result?.content?.find?.((c) => c.type === "text")?.text ?? "";
  if (result?.isError) throw new Error(`Snowflake tool error: ${text.slice(0, 500)}`);
  let parsed;
  try { parsed = JSON.parse(text); }
  catch { throw new Error(`Snowflake tool returned non-JSON: ${text.slice(0, 500)}`); }
  return parsed?.result_set?.data ?? [];
}

// ── Recipient query (PRD §6.1 — legacy Looker semantics) ─────────────────────

function recipientSql(propertyName) {
  const p = String(propertyName).replace(/'/g, "''");
  return `
SELECT r.RESERVATION_ID, p.PROPERTY_NAME, u.USER_FULL_NAME, u.USER_EMAIL,
       u.USER_PHONE, h.UNIT_NUMBER, p.MARKET_SEGMENT, ms.MS_AGM_NAME,
       ms.MS_TIMEZONE
FROM LANDING.CORE.DIMRESERVATION r
JOIN LANDING.CORE.DIMUSER u      ON u.USER_ID = r.RESERVATION_USER_ID
JOIN LANDING.CORE.DIMHOME h      ON h.HOME_ID = r.RESERVATION_HOME_ID
JOIN LANDING.CORE.DIMPROPERTY p  ON p.PROPERTY_ID = h.PROPERTY_ID
LEFT JOIN LANDING.CORE.DIMMARKETSEGMENT ms ON ms.MS_ID = p.MARKET_SEGMENT_ID
WHERE p.PROPERTY_NAME = '${p}'
  AND r.RESERVATION_PLATFORM = 'Landing'
  AND r.RESERVATION_CHECK_IN_DATE <= CURRENT_DATE
  AND (r.RESERVATION_CHECK_OUT_DATE IS NULL OR r.RESERVATION_CHECK_OUT_DATE >= CURRENT_DATE)
  AND h.HOME_CURRENTLY_OCCUPIED = TRUE
ORDER BY h.UNIT_NUMBER
LIMIT ${ROW_LIMIT}`;
}

// ── Normalization & dedupe (PRD §6.1) ────────────────────────────────────────

// USER_PHONE arrives in mixed formats: "(405) 441-3017", "+14355254840",
// "9518708735". Returns E.164 or null (null → SMS unavailable, email unaffected).
function toE164(raw) {
  if (!raw) return null;
  const s = String(raw).trim();
  const digits = s.replace(/[^\d]/g, "");
  if (s.startsWith("+") && digits.length >= 8 && digits.length <= 15) return "+" + digits;
  if (digits.length === 11 && digits.startsWith("1")) return "+" + digits;
  if (digits.length === 10) return "+1" + digits;
  return null;
}

// Group by lowercased email. First occurrence stays eligible (PENDING);
// duplicates become REVIEW (legacy dropped-or-alt-email juggling replaced by
// flag-don't-guess). Rows without a plausible email are dropped, counted.
function normalizeRows(rows) {
  const emailRe = /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/;
  const seen = new Map(); // lower email -> first reservation_id
  const out = [];
  let dropped = 0;

  for (const r of rows) {
    const [reservationId, , name, email, phoneRaw, unit, segment, agm, tz] = r;
    const cleanEmail = (email ?? "").toString().trim();
    if (!emailRe.test(cleanEmail)) { dropped++; continue; }
    const key = cleanEmail.toLowerCase();
    const dup = seen.get(key);
    if (!dup) seen.set(key, reservationId);
    out.push({
      reservation_id: String(reservationId ?? ""),
      email: cleanEmail,
      name: (name ?? "").toString().trim(),
      unit: (unit ?? "").toString().trim(),
      phone_e164: toE164(phoneRaw),
      phone_raw: (phoneRaw ?? "").toString().trim(),
      segment_timezone: (tz ?? "").toString().trim(),
      market_segment: (segment ?? "").toString().trim(),
      agm_name: (agm ?? "").toString().trim(),
      status: dup ? "REVIEW" : "PENDING",
      notes: dup ? `Duplicate email — also on reservation ${dup}` : "",
    });
  }
  return { recipients: out, dropped };
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
    const { property_name, campaign_id, callback_url, callback_token } = event.payload || {};
    const secrets = event.payload?._sandySecrets || {};

    try {
      await step.do("validate-inputs", async () => {
        if (!secrets.MCPGW_SNOWFLAKE_TOKEN) throw new Error("MCPGW_SNOWFLAKE_TOKEN secret missing");
        if (!property_name) throw new Error("property_name is required");
        if (!callback_url) throw new Error("callback_url is required");
        return { ok: true };
      });

      const rows = await step.do(
        "snowflake-fetch",
        { retries: { limit: 2, delay: "10 seconds", backoff: "exponential" } },
        async () => snowflakeSql(recipientSql(property_name), secrets)
      );

      const { recipients, dropped } = await step.do("normalize-dedupe", async () =>
        normalizeRows(rows)
      );

      await step.do(
        "send-callback",
        { retries: { limit: 2, delay: "30 seconds", backoff: "linear" } },
        async () => sendCallback(callback_url, {
          run_id: event.instanceId,
          callback_token,
          campaign_id,
          property_name,
          status: "complete",
          recipients,
          stats: {
            raw: rows.length,
            eligible: recipients.filter((r) => r.status === "PENDING").length,
            review: recipients.filter((r) => r.status === "REVIEW").length,
            dropped_invalid_email: dropped,
          },
          error: null,
        }, secrets)
      );

      return { ok: true, count: recipients.length };
    } catch (err) {
      // Best-effort error callback so the app can surface failure state.
      if (callback_url) {
        await step.do("send-error-callback", async () => sendCallback(callback_url, {
          run_id: event.instanceId,
          callback_token,
          campaign_id,
          property_name,
          status: "error",
          recipients: [],
          stats: null,
          error: String(err?.message || err),
        }, secrets));
      }
      throw err;
    }
  }
}

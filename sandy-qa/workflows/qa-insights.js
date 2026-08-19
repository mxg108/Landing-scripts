/**
 * qa-insights — the coaching-loop narrative engine (CoachingLoopSpec §8).
 *
 * Design split (same as qa-scoring-pipeline): the APP builds every fact
 * sheet and prompt (deterministic D1 aggregation — coachingFacts.ts); this
 * workflow is a thin durable executor that runs one Claude text call per
 * item and posts everything back in a single HMAC'd callback. No audio, no
 * npm deps, no D1 — plain fetch only.
 *
 * Modes ride the payload untouched: "progression" (one agent, the
 * dashboard card), "eom_batch" (one item per agent, the day-1 cron),
 * "coaching" / "team" (CL5). Model is PINNED by the app (claude-sonnet-5 —
 * judge parity, owner §11.5), never dynamic/sandy-workflows.
 *
 * Payload:
 * {
 *   mode, model: { model, max_tokens },
 *   items: [ { ref: {…opaque, echoed back…}, system, prompt } ],   // ≤ 40
 *   callback_url, callback_token
 * }
 * Callback: { run_id, status: "complete", mode, callback_token,
 *             items_out: [ { ref, ok, text?, usage?, error? } ] }
 *
 * Secrets: org AI_GATEWAY_TOKEN only (auto-injected; raw token in
 * cf-aig-authorization, NO Bearer).
 */

import { WorkflowEntrypoint } from "cloudflare:workers";

const AIG_ANTHROPIC =
  "https://gateway.ai.cloudflare.com/v1/d094105625bec434114c0b80ecfa7238/sandy-workflows/anthropic/v1/messages";

const RETRY = { retries: { limit: 1, delay: "10 seconds", backoff: "linear" } };
const MAX_ITEMS = 40;

async function sendCallback(callbackUrl, payload, sandySecrets) {
  const secret = sandySecrets?._sandyCallbackSecret;
  const bodyText = JSON.stringify(payload);
  const headers = { "Content-Type": "application/json" };
  if (secret) {
    const pathname = new URL(callbackUrl).pathname;
    const key = await crypto.subtle.importKey(
      "raw", new TextEncoder().encode(secret),
      { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
    const mac = await crypto.subtle.sign(
      "HMAC", key, new TextEncoder().encode(`POST|${pathname}|${bodyText}`));
    headers["X-Sandy-Workflow-Callback"] = Array.from(new Uint8Array(mac))
      .map((b) => b.toString(16).padStart(2, "0")).join("");
  }
  const res = await fetch(callbackUrl, {
    method: "POST", headers, body: bodyText,
    signal: AbortSignal.timeout(30000),
  });
  if (!res.ok) throw new Error(`Callback failed: HTTP ${res.status}`);
}

export class TenantWorkflow extends WorkflowEntrypoint {
  async run(event, step) {
    const p = event.payload ?? {};
    const aigKey = p._sandySecrets?.AI_GATEWAY_TOKEN;
    const items = (p.items ?? []).slice(0, MAX_ITEMS);
    const model = p.model?.model ?? "claude-sonnet-5";
    const maxTokens = p.model?.max_tokens ?? 3000;

    const itemsOut = [];
    for (let i = 0; i < items.length; i++) {
      const item = items[i];
      const out = await step.do(`claude-${i}`, RETRY, async () => {
        if (!aigKey) return { ok: false, error: "AI_GATEWAY_TOKEN missing" };
        const res = await fetch(AIG_ANTHROPIC, {
          method: "POST",
          headers: {
            "content-type": "application/json",
            "cf-aig-authorization": aigKey,
            "anthropic-version": "2023-06-01",
          },
          body: JSON.stringify({
            model,
            max_tokens: maxTokens,
            system: item.system,
            messages: [{ role: "user", content: item.prompt }],
          }),
          signal: AbortSignal.timeout(120000),
        });
        if (!res.ok) {
          const errText = (await res.text()).slice(0, 400);
          throw new Error(`gateway ${res.status}: ${errText}`);
        }
        const data = await res.json();
        const text = (data.content ?? [])
          .filter((b) => b.type === "text")
          .map((b) => b.text)
          .join("");
        return { ok: true, text, usage: data.usage ?? null, model: data.model ?? model };
      }).catch((err) => ({ ok: false, error: String(err?.message ?? err).slice(0, 400) }));
      itemsOut.push({ ref: item.ref ?? null, ...out });
    }

    await step.do("callback", RETRY, async () => {
      await sendCallback(
        p.callback_url,
        {
          run_id: event.instanceId,
          status: "complete",
          mode: p.mode ?? "progression",
          callback_token: p.callback_token,
          items_out: itemsOut,
        },
        p._sandySecrets
      );
      return { delivered: itemsOut.length };
    });

    return { ok: true, items: itemsOut.length };
  }
}

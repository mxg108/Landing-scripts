/**
 * qa-cron-ticker — durable continuation for the qa-scoring app's cron work
 * (sandy-qa/references/CronContinuation.md §2.2).
 *
 * Why: since 2026-09-14 the Sandy scheduler bounds how long a single app's
 * `/_sandy/cron` dispatch may take; every tick that carried real work
 * (nightly disposition sweep ≈ 2–3 min, Retell enqueues) was cut before it
 * finished. The app now acknowledges the cron instantly and this workflow
 * supplies the wall-clock: it loops "ask the app to do ONE bounded step,
 * then sleep" until the app reports `done`.
 *
 * Protocol (each tick is its own HMAC-signed callback request → a fresh
 * app request budget, independent of the cron dispatcher):
 *   POST callback_url {run_id, callback_token, status:"running", tick:i, reason}
 *   ← {ok, done:boolean, sleep_s:number}
 *   done → final callback {status:"complete", ticks, capped}
 *   otherwise step.sleep(sleep_s) and tick again (sleep clamped 10–120 s).
 * A response without `done === false` ENDS the loop (fail-safe: an app
 * version that does not speak the protocol never spins).
 *
 * One active run at a time (platform rule — the app treats a 409 on
 * trigger as "already running"). Runaway guard: max_iterations (default
 * 400); a capped run ends and the next hourly cron starts a fresh one.
 * A dead run (platform 5xx, cut request → step retry once, then error)
 * leaves all job state in the app's D1; the next cron re-triggers.
 *
 * Secrets: none (the callback secret is injected by the platform).
 * Payload: { callback_url, callback_token, max_iterations?, reason? }
 */

import { WorkflowEntrypoint } from "cloudflare:workers";

const RETRY = { retries: { limit: 1, delay: "10 seconds", backoff: "linear" } };
const DEFAULT_MAX_ITERATIONS = 400;
const MIN_SLEEP_S = 10;
const MAX_SLEEP_S = 120;
const DEFAULT_SLEEP_S = 30;

async function signedPost(callbackUrl, payload, sandySecrets, timeoutMs) {
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
  return await fetch(callbackUrl, {
    method: "POST", headers, body: bodyText,
    signal: AbortSignal.timeout(timeoutMs),
  });
}

export class TenantWorkflow extends WorkflowEntrypoint {
  async run(event, step) {
    const p = event.payload ?? {};
    if (!p.callback_url) return { status: "error", error: "no callback_url" };
    const max = Math.max(1, Math.min(1000, Number(p.max_iterations) || DEFAULT_MAX_ITERATIONS));
    const base = {
      run_id: event.instanceId,
      callback_token: p.callback_token ?? null,
      reason: p.reason ?? null,
    };

    let ticks = 0;
    let capped = false;
    let lastError = null;
    for (let i = 1; i <= max; i++) {
      let out;
      try {
        out = await step.do(`tick-${i}`, RETRY, async () => {
          const res = await signedPost(
            p.callback_url, { ...base, status: "running", tick: i }, p._sandySecrets, 90000);
          if (!res.ok) throw new Error(`tick ${i}: HTTP ${res.status}`);
          let json = {};
          try { json = await res.json(); } catch {}
          const done = json?.done === false ? false : true;
          const sleepRaw = Number(json?.sleep_s);
          const sleep_s = Math.max(MIN_SLEEP_S, Math.min(MAX_SLEEP_S,
            Number.isFinite(sleepRaw) && sleepRaw > 0 ? sleepRaw : DEFAULT_SLEEP_S));
          return { done, sleep_s };
        });
      } catch (err) {
        // Both attempts failed (cut request, platform 5xx). State lives in
        // the app's D1; end this run — the next cron tick re-triggers.
        lastError = String(err && err.message ? err.message : err).slice(0, 300);
        break;
      }
      ticks = i;
      if (out.done) break;
      if (i === max) { capped = true; break; }
      await step.sleep(`sleep-${i}`, `${out.sleep_s} seconds`);
    }

    await step.do("finish", async () => {
      try {
        await signedPost(
          p.callback_url,
          { ...base, status: lastError ? "error" : "complete", ticks, capped, error: lastError },
          p._sandySecrets, 30000);
      } catch {}
      return { delivered: true };
    });
    if (lastError) throw new Error(lastError);
    return { status: "complete", ticks, capped };
  }
}

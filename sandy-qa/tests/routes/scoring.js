// Harness stub for the worker's ../routes/scoring.js — the lib bundles are
// built with `--external:../routes/scoring.js`, so the dynamic import inside
// dispositionSweep.ts / retellSweep.ts resolves HERE (relative to
// tests/.build/). Records every auto-trigger; scripted statuses per call id.
export const calls = [];
export const statusByCall = new Map(); // call_id → HTTP status (default 200)

export async function autoScoreTrigger(_request, _db, teamId, _env, opts) {
  calls.push({ teamId, ...opts });
  const status = statusByCall.get(opts.callId) ?? 200;
  return new Response(JSON.stringify({ job_id: `score-${teamId}-${opts.callId}`, status: "queued" }), {
    status,
    headers: { "content-type": "application/json" },
  });
}

export async function drainScoreQueue() {
  return null;
}

export function isTransientProviderError() {
  return false;
}

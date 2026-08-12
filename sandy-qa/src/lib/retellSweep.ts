// Hourly Retell auto-pull (SofiaRetellSpec §8 R4) — rides the existing
// "7 * * * *" cron (Sandy caps at 2 schedules; no new schedule possible).
//
// Stateless by design: each sweep lists the newest ended Sofia calls and
// filters a trailing window, relying on D1 (existing eval / any prior
// queue row) for idempotency — no watermark table to drift. A call gets
// exactly ONE automatic attempt: if its job errors, it stays visible in
// the queue/console for a human re-score instead of retrying hourly
// forever. Voicemail and short calls are skipped (the provider's fetch
// guards would 422 them anyway — this avoids burning queue slots).

import { loadTeamConfig } from "./teamConfig.js";
import { listRetellCalls } from "./providers/retell.js";

const SWEEP_WINDOW_MS = 26 * 3600_000; // trailing window; overlap is idempotent
const MIN_DURATION_MS = 30_000; // short-call skip list (spec R4)
const MAX_ENQUEUES_PER_SWEEP = 10; // backlog surge guard on first activation

export interface SweepResult {
  skipped?: string;
  listed?: number;
  candidates?: number;
  enqueued?: number;
  existing?: number;
  errors?: number;
}

export async function sweepRetellCalls(
  db: D1Database,
  request: Request,
  env: {
    RETELL_API_KEY?: string;
    DIALPAD_API_KEY?: string;
    PULPO_MCP_URL?: string;
    PULPO_MCP_TOKEN?: string;
  }
): Promise<SweepResult> {
  if (!env.RETELL_API_KEY) return { skipped: "no_retell_key" };
  let config;
  try {
    config = await loadTeamConfig(db, "sofia");
  } catch {
    return { skipped: "no_sofia_team" };
  }
  if (config.provider !== "retell") return { skipped: "not_retell" };

  const roster = await db
    .prepare(
      "SELECT email, supervisor_email FROM qa_agents WHERE team_id = 'sofia' AND active = 1 LIMIT 1"
    )
    .first<any>();
  if (!roster?.email) return { skipped: "no_roster" };
  // Reviewer identity on auto-triggered evals = the roster supervisor
  // (Jackson) — same default the lookup page offers.
  const managerEmail = (roster.supervisor_email || roster.email).toLowerCase();

  const page = await listRetellCalls(env.RETELL_API_KEY, {
    agentIds: config.provider_config?.agent_ids ?? [],
    limit: 50,
  });
  const cutoff = Date.now() - SWEEP_WINDOW_MS;
  const candidates = page.items.filter(
    (c: any) =>
      c.call_id &&
      c.start_iso &&
      Date.parse(c.start_iso) >= cutoff &&
      c.in_voicemail !== true &&
      (c.duration_ms ?? 0) >= MIN_DURATION_MS
  );

  const { autoScoreTrigger } = await import("../routes/scoring.js");
  let enqueued = 0;
  let existing = 0;
  let errors = 0;
  for (const c of candidates) {
    if (enqueued >= MAX_ENQUEUES_PER_SWEEP) break;
    const scored = await db
      .prepare(
        "SELECT 1 AS x FROM qa_evaluations WHERE team_id = 'sofia' AND dialpad_call_id = ? LIMIT 1"
      )
      .bind(c.call_id)
      .first<any>();
    if (scored) {
      existing++;
      continue;
    }
    // ANY prior queue row (incl. done/error) = this call had its one
    // automatic attempt; humans re-score from the console/lookup.
    const attempted = await db
      .prepare(
        "SELECT 1 AS x FROM qa_score_queue WHERE team_id = 'sofia' AND call_id = ? LIMIT 1"
      )
      .bind(c.call_id)
      .first<any>();
    if (attempted) {
      existing++;
      continue;
    }
    try {
      const res = await autoScoreTrigger(request, db, "sofia", env, {
        callId: c.call_id,
        agentEmail: String(roster.email).toLowerCase(),
        managerEmail,
      });
      if (res.status === 200) enqueued++;
      else if (res.status === 409) existing++;
      else errors++;
    } catch {
      errors++;
    }
  }
  return {
    listed: page.items.length,
    candidates: candidates.length,
    enqueued,
    existing,
    errors,
  };
}

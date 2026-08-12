// On-platform cron work (ladder: crons slice).
//
// Two schedules (wrangler.toml [triggers].crons; Sandy caps at 2):
//   hourly "7 * * * *"  → queue pump. drainScoreQueue is otherwise pumped
//     by enqueue, callbacks, and status polls — this closes the one gap
//     where a lost callback + nobody watching left a queued job wedged
//     until someone next opened the console.
//   daily  "37 9 * * *" (09:37 UTC = early-morning LA) → maintenance:
//     prune terminal workflow_runs + qa_score_queue rows, old qa_events
//     (the SSE bus only ever tails NEW ids; history has no reader), and
//     cron_runs' own history. Then pumps the queue too.
//
// NOT here (still laptop-side until the sast_ push double-write / cutover):
// shadow_sync.py + nightly parity — both read Railway Postgres directly,
// which a Worker cron cannot.

const nowMinus = (mod: string) => `strftime('%Y-%m-%dT%H:%M:%fZ','now','${mod}')`;

export interface CronEnv {
  RETELL_API_KEY?: string;
  DIALPAD_API_KEY?: string;
  PULPO_MCP_URL?: string;
  PULPO_MCP_TOKEN?: string;
  GAS_WEBAPP_URL_SOFIA?: string;
}

export async function runHourlyPump(
  db: D1Database,
  request: Request,
  env: CronEnv = {}
): Promise<string> {
  // R4: discover + enqueue new ended Sofia calls BEFORE pumping, so a
  // fresh discovery can start on this same tick when the slot is free.
  let sweep: any;
  try {
    const { sweepRetellCalls } = await import("./retellSweep.js");
    sweep = await sweepRetellCalls(db, request, env);
  } catch (err) {
    sweep = { error: String((err as any)?.message ?? err).slice(0, 200) };
  }
  const { drainScoreQueue } = await import("../routes/scoring.js");
  const started = await drainScoreQueue(db, request);
  const queued = await db
    .prepare("SELECT COUNT(*) AS n FROM qa_score_queue WHERE status = 'queued'")
    .first<any>();
  return JSON.stringify({
    pumped: started ?? null,
    still_queued: queued?.n ?? 0,
    sweep,
  });
}

// Daily Sofia digest (R4, owner answer §9.5) — a summary POST to the
// sofia GAS webapp ({digest} payload branch), which delivers to Jackson
// via EMAIL.TO_OVERRIDE. Sent only when the last 24h had activity.
async function sofiaDigest(db: D1Database, gasUrl?: string): Promise<any> {
  if (!gasUrl) return { status: "skipped", message: "GAS_WEBAPP_URL_SOFIA not configured" };
  const one = async (sql: string) => (await db.prepare(sql).first<any>()) ?? {};
  const scored = await one(
    `SELECT COUNT(*) AS n FROM qa_evaluations
     WHERE team_id='sofia' AND created_at >= ${nowMinus("-1 day")}`
  );
  const approved = await one(
    `SELECT COUNT(*) AS n, ROUND(AVG(overall_score),1) AS avg FROM qa_evaluations
     WHERE team_id='sofia' AND approved_at >= ${nowMinus("-1 day")} AND overall_score IS NOT NULL`
  );
  const backlog = await one(
    `SELECT COUNT(*) AS n FROM qa_evaluations
     WHERE team_id='sofia' AND scoring_status='flagged_human_review'
       AND human_review_completed_at IS NULL`
  );
  const failures = await one(
    `SELECT COUNT(*) AS n FROM qa_score_queue
     WHERE team_id='sofia' AND status='error' AND finished_at >= ${nowMinus("-1 day")}`
  );
  const activity = (scored.n ?? 0) + (approved.n ?? 0) + (failures.n ?? 0);
  if (!activity) return { status: "skipped", message: "no sofia activity in 24h" };
  try {
    const res = await fetch(gasUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        digest: {
          team: "sofia",
          date: new Date().toISOString().slice(0, 10),
          scored_24h: scored.n ?? 0,
          approved_24h: approved.n ?? 0,
          avg_approved: approved.avg ?? null,
          backlog_pending: backlog.n ?? 0,
          queue_errors_24h: failures.n ?? 0,
          console_url: "https://qa-scoring.sandy.hellolanding.tech/score/sofia",
        },
      }),
      redirect: "follow",
      signal: AbortSignal.timeout(60_000),
    });
    const text = await res.text();
    try {
      return JSON.parse(text);
    } catch {
      return { status: "error", message: `non-JSON (HTTP ${res.status}): ${text.slice(0, 120)}` };
    }
  } catch (err) {
    return { status: "error", message: String((err as any)?.message ?? err).slice(0, 200) };
  }
}

export async function runDailyMaintenance(
  db: D1Database,
  request: Request,
  env: CronEnv = {}
): Promise<string> {
  const summary: Record<string, number> = {};
  const run = async (label: string, sql: string) => {
    const res = await db.prepare(sql).run();
    summary[label] = res.meta.changes ?? 0;
  };

  // Terminal job rows: the 409 double-score guard lives on qa_evaluations,
  // not here, so old complete/error runs are safely prunable. In-flight
  // rows (queued/pending/running) are never touched.
  await run(
    "workflow_runs",
    `DELETE FROM workflow_runs WHERE status IN ('complete','error') AND created_at < ${nowMinus("-14 days")}`
  );
  await run(
    "score_queue",
    `DELETE FROM qa_score_queue WHERE status IN ('done','error') AND enqueued_at < ${nowMinus("-14 days")}`
  );
  // SSE bus: consumers tail ids from "now" (initial cursor = max id);
  // reconnects only ever look back seconds. Week-old events are dead rows.
  await run(
    "qa_events",
    `DELETE FROM qa_events WHERE created_at < ${nowMinus("-7 days")}`
  );
  await run(
    "cron_runs",
    `DELETE FROM cron_runs WHERE ran_at < ${nowMinus("-30 days")}`
  );

  const digest = await sofiaDigest(db, env.GAS_WEBAPP_URL_SOFIA);

  const { drainScoreQueue } = await import("../routes/scoring.js");
  const started = await drainScoreQueue(db, request);
  return JSON.stringify({ pruned: summary, pumped: started ?? null, digest });
}

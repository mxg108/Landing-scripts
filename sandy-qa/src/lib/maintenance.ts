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

export async function runHourlyPump(
  db: D1Database,
  request: Request
): Promise<string> {
  const { drainScoreQueue } = await import("../routes/scoring.js");
  const started = await drainScoreQueue(db, request);
  const queued = await db
    .prepare("SELECT COUNT(*) AS n FROM qa_score_queue WHERE status = 'queued'")
    .first<any>();
  return JSON.stringify({
    pumped: started ?? null,
    still_queued: queued?.n ?? 0,
  });
}

export async function runDailyMaintenance(
  db: D1Database,
  request: Request
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

  const { drainScoreQueue } = await import("../routes/scoring.js");
  const started = await drainScoreQueue(db, request);
  return JSON.stringify({ pruned: summary, pumped: started ?? null });
}

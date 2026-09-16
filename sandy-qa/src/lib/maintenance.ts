// On-platform cron work (ladder: crons slice; CronContinuation.md for the
// 2026-09-16 bounded-tick refactor).
//
// Two schedules (wrangler.toml [triggers].crons; Sandy caps at 2):
//   hourly "7 * * * *"  → light tick: trigger the qa-cron-ticker workflow
//     (the durable continuation that runs the sweeps, the EOD report and
//     the queue pump as bounded steps); if it cannot be triggered, run ONE
//     ticker step inline as the floor ("at least one unit of work/hour").
//   daily  "37 9 * * *" (09:37 UTC = early-morning LA) → maintenance:
//     prune terminal workflow_runs + qa_score_queue rows, old qa_events
//     (the SSE bus only ever tails NEW ids; history has no reader), and
//     cron_runs' own history. Then the Sofia digest, the EOM branch on the
//     1st, and the ticker trigger.
//
// Since 2026-09-14 the Sandy scheduler bounds how long a single app's
// cron dispatch may take (release note "Scheduled crons now fire
// reliably"); every tick that carried minutes of work was cut before its
// cron_runs INSERT. The handler in index.tsx now inserts the row FIRST,
// acknowledges instantly, and runs these functions inside ctx.waitUntil
// (~30 s budget) — so everything here must stay light.
//
// NOT here (still laptop-side until the sast_ push double-write / cutover):
// shadow_sync.py + nightly parity — both read Railway Postgres directly,
// which a Worker cron cannot.

const nowMinus = (mod: string) => `strftime('%Y-%m-%dT%H:%M:%fZ','now','${mod}')`;

export const TICKER_WORKFLOW_NAME = "qa-cron-ticker";
// Fallback when the app-workflows listing is unavailable (same pattern as
// the scoring drain's pinned pipeline id). Filled at workflow creation.
export const TICKER_WORKFLOW_ID_FALLBACK = "ab006dfa-1656-43c0-ba08-3264fa797d03";
const TICKER_MAX_ITERATIONS = 400; // ≈ 3 h at the queue-wait cadence
const SLEEP_PROGRESSING_S = 15; // a sweep/EOD phase just advanced
const SLEEP_QUEUE_WAIT_S = 45; // only waiting on the scoring queue

export interface CronEnv {
  RETELL_API_KEY?: string;
  DIALPAD_API_KEY?: string;
  PULPO_MCP_URL?: string;
  PULPO_MCP_TOKEN?: string;
  GAS_WEBAPP_URL_SOFIA?: string;
  GAS_WEBAPP_URL_HR?: string;
  // ShiftReport §10: Google service-account JSON for the EOD sheet sink.
  GSHEETS_SA_JSON?: string;
}

export interface TickerStep {
  done: boolean;
  sleep_s: number;
  summary: Record<string, any>;
}

// ── ticker step (CronContinuation §2.3) ─────────────────────────────────────
// ONE bounded unit of work per pending job, each guarded (never throws).
// Called by the qa-cron-ticker callback every ~15–45 s and, as the
// fallback floor, once per hourly tick when the ticker cannot be triggered.
export async function runTickerStep(
  db: D1Database,
  request: Request,
  env: CronEnv = {}
): Promise<TickerStep> {
  const summary: Record<string, any> = {};
  let progressing = false;

  // 0. One-shot cron jobs (EOM assessments, HR bonus dispatch — CC4): the
  //    oldest pending job advances one handler step.
  try {
    const { runCronJobsStep } = await import("./cronJobs.js");
    summary.jobs = await runCronJobsStep(db, request, env);
    if (summary.jobs?.more) progressing = true;
  } catch (err) {
    summary.jobs = { error: String((err as any)?.message ?? err).slice(0, 200) };
  }
  // 1. Nightly disposition sweep — one phase transition per team.
  try {
    const { sweepDispositions, sweepHasMore } = await import("./dispositionSweep.js");
    summary.dispositions = await sweepDispositions(db, request, env);
    if (sweepHasMore(summary.dispositions)) progressing = true;
  } catch (err) {
    summary.dispositions = { error: String((err as any)?.message ?? err).slice(0, 200) };
  }
  // 2. Retell (Sofia) sweep — list + ≤3 enqueues.
  try {
    const { sweepRetellCalls } = await import("./retellSweep.js");
    summary.sweep = await sweepRetellCalls(db, request, env);
    if (summary.sweep?.more) progressing = true;
  } catch (err) {
    summary.sweep = { error: String((err as any)?.message ?? err).slice(0, 200) };
  }
  // 3. EOD Google-Sheet report — one bounded poll pass (13–19 UTC gate).
  try {
    const { runEodReports } = await import("./eodReport.js");
    summary.eod = await runEodReports(db, env);
    if (Object.values(summary.eod ?? {}).some((r: any) => r?.more === true)) progressing = true;
  } catch (err) {
    summary.eod = { error: String((err as any)?.message ?? err).slice(0, 200) };
  }
  // 4. Queue pump — one trigger when the single platform slot is free.
  let queued = 0;
  try {
    const { drainScoreQueue } = await import("../routes/scoring.js");
    summary.pumped = (await drainScoreQueue(db, request, env)) ?? null;
    const counts = await db
      .prepare(
        `SELECT SUM(CASE WHEN status='queued' THEN 1 ELSE 0 END) AS queued,
                SUM(CASE WHEN status IN ('triggering','running') THEN 1 ELSE 0 END) AS active
         FROM qa_score_queue WHERE status IN ('queued','triggering','running')`
      )
      .first<any>();
    queued = Number(counts?.queued ?? 0);
    summary.still_queued = queued;
    summary.active = Number(counts?.active ?? 0);
  } catch (err) {
    summary.pump_error = String((err as any)?.message ?? err).slice(0, 200);
  }

  const more = progressing || queued > 0;
  return {
    done: !more,
    sleep_s: progressing ? SLEEP_PROGRESSING_S : SLEEP_QUEUE_WAIT_S,
    summary,
  };
}

// ── ticker trigger ──────────────────────────────────────────────────────────
export async function triggerTicker(
  db: D1Database,
  request: Request,
  reason: string
): Promise<Record<string, any>> {
  try {
    const { listTriggerableWorkflows, triggerWorkflowWithCallback } = await import(
      "../workflow.js"
    );
    const known = await listTriggerableWorkflows();
    const wfId =
      known.find((w: any) => w.name === TICKER_WORKFLOW_NAME)?.id ?? TICKER_WORKFLOW_ID_FALLBACK;
    const run = await triggerWorkflowWithCallback(wfId, TICKER_WORKFLOW_NAME, request, {
      reason,
      max_iterations: TICKER_MAX_ITERATIONS,
    });
    try {
      await db
        .prepare(
          "INSERT INTO workflow_runs (run_id, workflow_name, status, result) VALUES (?, ?, 'running', ?) ON CONFLICT(run_id) DO NOTHING"
        )
        .bind(run.id, TICKER_WORKFLOW_NAME, JSON.stringify({ reason, started_at: new Date().toISOString() }))
        .run();
    } catch {}
    return { status: "triggered", run_id: run.id };
  } catch (err) {
    const msg = String((err as any)?.message ?? err);
    // One active run per workflow (platform rule): a 409 means the ticker
    // is already working — nothing to do.
    if (/trigger failed 409/.test(msg)) return { status: "already_running" };
    return { status: "error", note: msg.slice(0, 200) };
  }
}

export async function runHourlyPump(
  db: D1Database,
  request: Request,
  env: CronEnv = {}
): Promise<string> {
  const ticker = await triggerTicker(db, request, "hourly");
  let step: TickerStep | null = null;
  if (ticker.status !== "triggered" && ticker.status !== "already_running") {
    // Floor: the ticker is unavailable — do one bounded unit of work now
    // (what a pre-continuation tick did, minus the multi-minute parts).
    step = await runTickerStep(db, request, env);
  }
  const queued = await db
    .prepare("SELECT COUNT(*) AS n FROM qa_score_queue WHERE status = 'queued'")
    .first<any>();
  return JSON.stringify({
    ticker,
    ...(step ? { step: step.summary, step_done: step.done } : {}),
    still_queued: queued?.n ?? 0,
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
      // The daily tick runs inside a ~30 s waitUntil continuation now
      // (CronContinuation §2.1) — a slow GAS must not eat the whole budget.
      signal: AbortSignal.timeout(20_000),
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

  // EOM (CoachingLoopSpec §8 CL4 + HRBonusSheet §6): on the 1st (LA) the
  // closed month's progression assessments (one qa-insights batch run; the
  // builder skips agents already covered) and the HR-bonus workbook push to
  // GAS (tabs rewrite in place). Both used to run inline here; since the
  // daily tick lives inside a ~30 s waitUntil continuation they are now
  // qa_cron_jobs rows executed by the ticker one bounded step at a time
  // (CronContinuation §2.6 / CC4, src/lib/cronJobs.ts). Re-run a month by
  // inserting a fresh row.
  let eom: any = null;
  const laDay = new Intl.DateTimeFormat("sv-SE", {
    timeZone: "America/Los_Angeles",
  }).format(new Date());
  if (laDay.endsWith("-01")) {
    const [y, m] = laDay.slice(0, 7).split("-").map(Number);
    const closed = m === 1 ? `${y - 1}-12` : `${y}-${String(m - 1).padStart(2, "0")}`;
    try {
      const { enqueueCronJob } = await import("./cronJobs.js");
      eom = {
        month: closed,
        eom_assessments: await enqueueCronJob(db, "eom_assessments", closed),
        hr_bonus: await enqueueCronJob(db, "hr_bonus", closed),
      };
    } catch (err) {
      eom = { month: closed, error: String((err as any)?.message ?? err).slice(0, 200) };
    }
  }

  const ticker = await triggerTicker(db, request, "daily");
  return JSON.stringify({
    pruned: summary, ticker, digest,
    ...(eom ? { eom } : {}),
  });
}

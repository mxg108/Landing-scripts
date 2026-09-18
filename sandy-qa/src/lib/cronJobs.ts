// One-shot cron jobs as ticker steps (CronContinuation.md §2.6 / CC4).
//
// A qa_cron_jobs row = one unit of monthly work (job, key) — enqueued by the
// daily tick on the 1st, executed by runTickerStep one bounded step at a
// time. Handlers are pure "advance one step" functions over (row.phase,
// row.cursor, row.report); they never throw past the runner, which stamps
// `error` with the message. Re-runs: INSERT a new row or reset an old one
// to 'pending' (both jobs are idempotent by design — see the handlers).

export interface CronJobRow {
  id: number;
  job: string;
  key: string;
  status: string;
  phase: string | null;
  cursor: number;
  attempts: number;
  report: string | null;
}

export interface JobStepResult {
  done: boolean;
  phase?: string | null;
  cursor?: number;
  report?: any;
  // Waiting on something external (a queue draining, a workflow run):
  // the runner stamps next_at = now + wait_s and neither picks nor counts
  // the row until then, so the ticker sleeps instead of spinning.
  wait_s?: number;
}

export type JobHandler = (
  db: D1Database,
  request: Request,
  env: Record<string, any>,
  row: CronJobRow,
  report: any
) => Promise<JobStepResult>;

const MAX_ATTEMPTS = 3; // a step that throws is retried on later ticks, then error

// ── handlers ───────────────────────────────────────────────────────────────

// EOM assessments (CoachingLoopSpec §8, CL4): one qa-insights batch run for
// the closed month; the batch builder skips agents already covered (the
// guard that makes re-runs safe). Single step.
const eomAssessments: JobHandler = async (db, request, _env, row) => {
  const { eomAssessmentBatch } = await import("../routes/insights.js");
  const res = await eomAssessmentBatch(db, request, ["member_support", "sales", "sofia"], row.key);
  return { done: true, report: res };
};

// HR bonus workbook (HRBonusSheet §6, Sandy-era transport): one TEAM per
// step (cursor = index into teams with hr_export) — the GAS render POST is
// the slow part (10–30 s). Tabs rewrite in place, so re-runs are safe.
const hrBonus: JobHandler = async (db, _request, env, row, report) => {
  const teams = (
    await db
      .prepare("SELECT id, hr_export FROM teams WHERE hr_export IS NOT NULL ORDER BY id")
      .all<any>()
  ).results;
  const out = { ...(report ?? {}) };
  const cursor = Number(row.cursor ?? 0);
  if (cursor >= teams.length) return { done: true, report: out, cursor };
  const t = teams[cursor];
  const { hrExportFor, fetchMonthPayload, dispatchHrBonus } = await import("./hrBonus.js");
  const { loadTeamConfig } = await import("./teamConfig.js");
  const hr = hrExportFor(t.hr_export);
  if (hr) {
    const config = await loadTeamConfig(db, t.id);
    const payload = await fetchMonthPayload(db, config, hr, row.key);
    const receipt = await dispatchHrBonus(payload, env.GAS_WEBAPP_URL_HR);
    out[t.id] = { month: row.key, agents: payload.agents.length, gas_receipt: receipt };
  } else {
    out[t.id] = { skipped: "hr_export not parseable" };
  }
  return { done: cursor + 1 >= teams.length, cursor: cursor + 1, report: out };
};

// Daily agent QA digest (DailyDigest.md): enqueued by the nightly sweep's
// done transition, key '<team>:<pull_date>'; waits for the picks to score,
// summarizes via qa-insights, sends one email per agent.
const dailyDigest: JobHandler = async (db, request, env, row, report) => {
  const { dailyDigestJob } = await import("./dailyDigest.js");
  return dailyDigestJob(db, request, env, row, report);
};

export const DEFAULT_HANDLERS: Record<string, JobHandler> = {
  eom_assessments: eomAssessments,
  hr_bonus: hrBonus,
  daily_digest: dailyDigest,
};

// ── queue ──────────────────────────────────────────────────────────────────

export async function enqueueCronJob(
  db: D1Database,
  job: string,
  key: string
): Promise<{ queued: boolean }> {
  const res = await db
    .prepare("INSERT INTO qa_cron_jobs (job, key) VALUES (?, ?) ON CONFLICT(job, key) DO NOTHING")
    .bind(job, key)
    .run();
  return { queued: !!res.meta.changes };
}

const parseJson = (text: string | null | undefined, fallback: any) => {
  if (!text) return fallback;
  try {
    return JSON.parse(text);
  } catch {
    return fallback;
  }
};

// ONE bounded step: the oldest pending/running job advances one handler
// step. Returns `more` while any runnable row remains.
export async function runCronJobsStep(
  db: D1Database,
  request: Request,
  env: Record<string, any>,
  handlers: Record<string, JobHandler> = DEFAULT_HANDLERS
): Promise<Record<string, any>> {
  const nowIso = new Date().toISOString();
  const row = await db
    .prepare(
      "SELECT * FROM qa_cron_jobs WHERE status IN ('pending','running') AND (next_at IS NULL OR next_at <= ?) ORDER BY id LIMIT 1"
    )
    .bind(nowIso)
    .first<CronJobRow>();
  if (!row) return { more: false };

  // Rows parked by wait_s (next_at in the future) are not "more" — the
  // hourly cron re-enters and picks them up once their wake-up passes.
  const remaining = async () => {
    const r = await db
      .prepare(
        "SELECT COUNT(*) AS n FROM qa_cron_jobs WHERE status IN ('pending','running') AND (next_at IS NULL OR next_at <= ?)"
      )
      .bind(new Date().toISOString())
      .first<any>();
    return Number(r?.n ?? 0) > 0;
  };

  const handler = handlers[row.job];
  if (!handler) {
    await db
      .prepare("UPDATE qa_cron_jobs SET status = 'error', report = ?, updated_at = ? WHERE id = ?")
      .bind(JSON.stringify({ error: `unknown job ${row.job}` }), nowIso, row.id)
      .run();
    return { job: row.job, key: row.key, status: "error", error: "unknown job", more: await remaining() };
  }

  if (row.status === "pending") {
    await db
      .prepare("UPDATE qa_cron_jobs SET status = 'running', updated_at = ? WHERE id = ?")
      .bind(nowIso, row.id)
      .run();
  }
  const report = parseJson(row.report, {});
  let step: JobStepResult;
  try {
    step = await handler(db, request, env, row, report);
  } catch (err) {
    const attempts = Number(row.attempts ?? 0) + 1;
    const msg = String((err as any)?.message ?? err).slice(0, 300);
    const terminal = attempts >= MAX_ATTEMPTS;
    await db
      .prepare(
        "UPDATE qa_cron_jobs SET status = ?, attempts = ?, report = ?, updated_at = ? WHERE id = ?"
      )
      .bind(
        terminal ? "error" : "running",
        attempts,
        JSON.stringify({ ...report, last_error: msg }),
        nowIso,
        row.id
      )
      .run();
    return {
      job: row.job, key: row.key, status: terminal ? "error" : "retry",
      attempts, error: msg, more: await remaining(),
    };
  }
  const cursor = step.cursor ?? row.cursor ?? 0;
  const phase = step.phase === undefined ? row.phase : step.phase;
  const nextAt =
    !step.done && step.wait_s && step.wait_s > 0
      ? new Date(Date.now() + step.wait_s * 1000).toISOString()
      : null;
  await db
    .prepare(
      "UPDATE qa_cron_jobs SET status = ?, phase = ?, cursor = ?, report = ?, next_at = ?, updated_at = ? WHERE id = ?"
    )
    .bind(
      step.done ? "completed" : "running",
      phase,
      cursor,
      JSON.stringify(step.report ?? report ?? {}),
      nextAt,
      nowIso,
      row.id
    )
    .run();
  return {
    job: row.job, key: row.key, status: step.done ? "completed" : "running",
    phase, cursor, ...(nextAt ? { next_at: nextAt } : {}),
    more: step.done || nextAt ? await remaining() : true,
  };
}

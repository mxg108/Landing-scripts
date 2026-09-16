-- 0018 — one-shot cron jobs run as qa-cron-ticker steps (CronContinuation
-- §2.6 / CC4). The daily tick used to run the EOM assessments trigger and
-- the HR-bonus GAS dispatch inline; both now live inside a ~30 s waitUntil
-- continuation, and the GAS POST alone can take 10–30 s. The daily tick now
-- enqueues one row per job and the ticker executes them one bounded step
-- at a time (hr_bonus = one team per step; cursor = team index).
--
-- Re-runs are safe (assessments guard on existing coverage; the HR tabs
-- rewrite in place): to re-run a month, INSERT a fresh row (or set an old
-- one back to 'pending', cursor 0). Sandy-only, never synced.
CREATE TABLE qa_cron_jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job         TEXT NOT NULL,             -- eom_assessments | hr_bonus
    key         TEXT NOT NULL,             -- closed month YYYY-MM
    status      TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending','running','completed','error')),
    phase       TEXT,
    cursor      INTEGER NOT NULL DEFAULT 0,
    attempts    INTEGER NOT NULL DEFAULT 0,
    report      TEXT CHECK (report IS NULL OR json_valid(report)),
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    CONSTRAINT uq_cron_jobs UNIQUE (job, key)
);

-- 0004_cron_log.sql — observability for the on-platform crons (ladder:
-- crons slice). Each /_sandy/cron dispatch appends a row; the daily
-- maintenance prunes its own history. This is how we VERIFY fires without
-- platform-side cron logs (and how a silent cron outage becomes visible:
-- the newest row goes stale).

CREATE TABLE cron_runs (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    cron    TEXT NOT NULL,          -- the schedule expression that fired
    ran_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    note    TEXT                    -- what the run did (JSON summary)
);

CREATE INDEX idx_cron_runs_ran_at ON cron_runs (ran_at DESC);

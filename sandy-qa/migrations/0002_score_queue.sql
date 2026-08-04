-- 0002_score_queue.sql — serialize qa-scoring-pipeline triggers.
-- The Sandy platform allows ONE active run per workflow: concurrent triggers
-- fail with 409 "Workflow already has an active run" (observed on the first
-- batch submit from the scoring console). POST /score therefore always
-- ENQUEUES here; a CAS-guarded drain (scoring.ts drainScoreQueue) triggers
-- the head job only when nothing is in flight, pumped from enqueue, the
-- scoring callback, and the status polls.

CREATE TABLE qa_score_queue (
    job_id        TEXT PRIMARY KEY,   -- score-{team}-{call}-{agent} (same key as workflow_runs)
    team_id       TEXT NOT NULL,
    call_id       TEXT NOT NULL,
    payload       TEXT NOT NULL,      -- full workflow trigger params, built at enqueue time
    status        TEXT NOT NULL
        CHECK (status IN ('queued','triggering','running','done','error')),
    attempts      INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT,
    sandy_run_id  TEXT,
    enqueued_at   TEXT NOT NULL,
    triggered_at  TEXT,
    finished_at   TEXT
);

CREATE INDEX idx_score_queue_status ON qa_score_queue (status, enqueued_at);

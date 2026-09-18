-- 0021_daily_digest.sql — Daily Agent QA Digest (references/DailyDigest.md
-- §2). One qa_agent_digests row per (team, sweep night, agent): the
-- deterministic fact sheet the summarizer saw, the model's summary, and the
-- email receipt. Sandy-only, never synced.
--
-- qa_cron_jobs.next_at: a handler that is WAITING (queue still draining,
-- summary run in flight) stamps a wake-up time; the runner neither picks nor
-- counts such rows until then, so the ticker sleeps instead of spinning.

CREATE TABLE qa_agent_digests (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id        TEXT NOT NULL REFERENCES teams(id),
    pull_date      TEXT NOT NULL,                -- qa_disposition_pulls.pull_date (team-local day swept)
    agent_id       INTEGER NOT NULL REFERENCES qa_agents(id),
    agent_email    TEXT NOT NULL,
    agent_name     TEXT NOT NULL,
    eval_ids       TEXT NOT NULL CHECK (json_valid(eval_ids)),      -- finalized evals from the night's picks
    facts          TEXT NOT NULL CHECK (json_valid(facts)),         -- §3 fact sheet (what the model saw)
    summary        TEXT CHECK (summary IS NULL OR json_valid(summary)),  -- §4 validated model output
    summary_model  TEXT,
    summary_usage  TEXT CHECK (summary_usage IS NULL OR json_valid(summary_usage)),
    summary_error  TEXT,                          -- model/JSON failure → facts-only email
    email_status   TEXT NOT NULL DEFAULT 'pending'
                   CHECK (email_status IN ('pending','ok','skipped','error')),
    email_message  TEXT,
    sent_at        TEXT,
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    CONSTRAINT uq_agent_digest UNIQUE (team_id, pull_date, agent_id)
);
CREATE INDEX idx_agent_digests_night ON qa_agent_digests (team_id, pull_date);

ALTER TABLE qa_cron_jobs ADD COLUMN next_at TEXT;

-- Member Support opts in (DailyDigest §0 defaults). send_deadline_utc is a
-- decimal hour: 13.5 = 13:30 UTC ≈ 07:30 America/Mexico_City.
UPDATE teams
SET provider_config = json_set(
  provider_config,
  '$.nightly_sweep.digest',
  json('{"enabled": true, "send_deadline_utc": 13.5, "model": "claude-sonnet-5", "max_tokens": 6000, "cc_supervisor": false}')
)
WHERE id = 'member_support' AND provider_config IS NOT NULL;

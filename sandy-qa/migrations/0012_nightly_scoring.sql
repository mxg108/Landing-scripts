-- 0012_nightly_scoring.sql — NightlyScoring §3 (owner sign-off 2026-08-30).
-- Nightly Member Support auto-scoring: one row per (team, local day) is the
-- re-run latch, the resume handle for slow Stats-API exports, and the audit
-- trail of every sweep. Sandy-only (never synced) — same stance as
-- qa_roster_events / cron_runs.
CREATE TABLE qa_disposition_pulls (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id      TEXT NOT NULL REFERENCES teams(id),
    pull_date    TEXT NOT NULL,             -- local day exported (YYYY-MM-DD, team tz)
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending','fetching','completed','error')),
    request_id   TEXT,                      -- Stats API export id (resume handle)
    report       TEXT CHECK (report IS NULL OR json_valid(report)),
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    CONSTRAINT uq_pulls_team_date UNIQUE (team_id, pull_date)
);

-- Sweep config rides teams.provider_config (house pattern: sofia's
-- agent_ids live there). member_support is provider 'dialpad' with a NULL
-- provider_config today; Sales onboards later by config alone.
-- local_hour_utc 6 = 00:07 America/Mexico_City (fixed UTC-6, no DST).
UPDATE teams SET provider_config = json('{
  "callcenter_id": "5699048497577984",
  "nightly_sweep": {
    "enabled": true,
    "per_agent": 3,
    "min_duration_s": 240,
    "max_duration_s": 1800,
    "suppress_email": true,
    "reviewer_email": "qa-system@hellolanding.com",
    "timezone": "America/Mexico_City",
    "local_hour_utc": 6,
    "max_enqueues": 120
  }
}') WHERE id = 'member_support';

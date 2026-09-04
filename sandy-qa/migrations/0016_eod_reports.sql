-- 0016_eod_reports.sql — ShiftReport §10 (EOD Google Sheet sink, 2026-09-03).
-- One row per (team, local report date) is the re-run latch, the resume
-- handle for the Stats-API exports (request ids keyed by selector), and the
-- audit trail (aggregates only — caller numbers never land in D1). Sandy-only
-- (never synced), same stance as qa_disposition_pulls / cron_runs.
CREATE TABLE qa_eod_reports (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id      TEXT NOT NULL REFERENCES teams(id),
    report_date  TEXT NOT NULL,             -- local day reported (YYYY-MM-DD, team tz)
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending','fetching','completed','error')),
    export_ids   TEXT CHECK (export_ids IS NULL OR json_valid(export_ids)),
                                            -- {"calls:1-1":"<request_id>","calls:today":…}
    report       TEXT CHECK (report IS NULL OR json_valid(report)),
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    CONSTRAINT uq_eod_team_date UNIQUE (team_id, report_date)
);

-- Config rides teams.provider_config (house pattern; callcenter_id +
-- nightly_sweep already there). local_hour_utc 13 = 07:07 America/Mexico_City:
-- the night shift ends 06:00 and Dialpad's is_today tables refresh every
-- 30 min, so the 06:07 tick can miss the last half hour (§10.2).
UPDATE teams SET provider_config = json_set(COALESCE(provider_config, '{}'), '$.eod_sheet', json('{
  "enabled": true,
  "spreadsheet_id": "1IF2vpb7oo3gybCkX82Wmk1YtYuwwszFqwIDHxy_032Y",
  "timezone": "America/Mexico_City",
  "local_hour_utc": 13,
  "catchup_hours": 6,
  "short_abandon_s": 6,
  "sl_seconds_fallback": 30,
  "sl_target_pct_fallback": 80,
  "shifts": [
    {"key": "morning",   "label": "Morning",   "start": "06:00", "end": "16:00"},
    {"key": "afternoon", "label": "Afternoon", "start": "12:30", "end": "22:00"},
    {"key": "night",     "label": "Night",     "start": "22:00", "end": "06:00"}
  ]
}')) WHERE id = 'member_support';

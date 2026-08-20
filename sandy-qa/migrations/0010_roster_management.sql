-- 0010_roster_management.sql — AgentAddition §4 (AA1).
-- Roster management on Sandy: departure semantics on qa_agents (soft,
-- reversible — rehires are common; `on_leave` is a departure REASON, not a
-- third state: active stays the single behavioral switch every consumer
-- already respects) + qa_roster_events history (Sandy-only, never synced —
-- the rehire story survives, and "who changed the roster" is answerable).
-- Companion to AA0: sync_agents_upsert is INSERT-only from AA0 on, so these
-- D1-only columns are safe under the sync (its column list comes from PG's
-- information_schema and never touches D1 extras).

-- ── qa_agents: departure stamps ──────────────────────────────────────────
ALTER TABLE qa_agents ADD COLUMN departure_reason TEXT
    CHECK (departure_reason IS NULL OR departure_reason IN
        ('left_company','other_team','on_leave','terminated','other'));
ALTER TABLE qa_agents ADD COLUMN departed_at TEXT;      -- ISO, LA-day honest
ALTER TABLE qa_agents ADD COLUMN departure_note TEXT;

-- ── qa_roster_events: every add/depart/rehire/edit/reassign as an event ──
-- Mirrors the coaching-audit stance (qa_score_audit's CHECK enum is closed;
-- own table, like cron_runs). agent_id is a soft ref — agents never delete.
CREATE TABLE qa_roster_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id    INTEGER NOT NULL,
    team_id     TEXT NOT NULL REFERENCES teams(id),
    action      TEXT NOT NULL CHECK (action IN
                    ('added','departed','rehired','edited','supervisor_changed')),
    detail      TEXT CHECK (detail IS NULL OR json_valid(detail)),
    actor_email TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX idx_roster_events_agent ON qa_roster_events (agent_id, created_at DESC);
CREATE INDEX idx_roster_events_team ON qa_roster_events (team_id, created_at DESC);

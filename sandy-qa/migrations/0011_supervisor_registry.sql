-- 0011_supervisor_registry.sql — AgentAddition follow-up (owner ask,
-- 2026-08-20): the Manage-roster card must be able to ADD supervisors, not
-- just reassign agents between existing ones. A supervisor "exists" today
-- only as a distinct qa_agents.supervisor_email string, so a new supervisor
-- with no agents assigned yet had no way into any picker. Sandy-only
-- registry (never synced); the supervisors endpoint unions this with the
-- derived set. `label` is the string agent rows carry (legacy rows hold
-- first names — 'Max', 'Andrei'); `email` is optional and enables the
-- /admin manager grant cross-link + future notifications.
CREATE TABLE qa_supervisors (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id     TEXT NOT NULL REFERENCES teams(id),
    label       TEXT NOT NULL CHECK (length(trim(label)) > 0),
    email       TEXT,
    active      INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
    created_by  TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE UNIQUE INDEX uq_supervisors_team_lower_label
    ON qa_supervisors (team_id, lower(label));

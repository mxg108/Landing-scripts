-- 0019_supervisor_deliverables.sql — Supervisor Deliverables
-- (references/SupervisorDeliverables.md §3; Member Support Direction's
-- "Supervisor Deliverables & Responsibilities" index is the authority for
-- WHAT each row carries).
--
-- One qa_sup_reports row per deliverable instance: the end-of-shift report
-- (daily, per shift), the weekly performance review (per supervisor), the
-- bi-weekly Tuesday tracker (team) and the monthly abandon-rate review (per
-- supervisor). `auto` is the machine-computed fact snapshot frozen at
-- submit; `manual` is what only the supervisor knows (why it happened, what
-- was done, what is next); `published` is the push trail (Slack ts, sheet
-- tab, …). Sandy-only, never synced — same stance as qa_eod_reports.
CREATE TABLE qa_sup_reports (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id       TEXT NOT NULL REFERENCES teams(id),
    kind          TEXT NOT NULL CHECK (kind IN ('daily','weekly','biweekly','monthly')),
    period_key    TEXT NOT NULL,          -- daily: YYYY-MM-DD · weekly: YYYY-Www · biweekly: meeting YYYY-MM-DD · monthly: YYYY-MM
    shift         TEXT,                   -- daily only: eod_sheet shift key (morning|afternoon|night)
    supervisor    TEXT,                   -- roster label (qa_agents.supervisor_email vocabulary); NULL = whole team
    owner_email   TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','submitted')),
    auto          TEXT CHECK (auto IS NULL OR json_valid(auto)),
    manual        TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(manual)),
    published     TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(published)),
    submitted_at  TEXT,
    submitted_by  TEXT,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE UNIQUE INDEX uq_sup_reports_instance
    ON qa_sup_reports (team_id, kind, period_key, COALESCE(shift,''), COALESCE(supervisor,''));
CREATE INDEX idx_sup_reports_team_kind ON qa_sup_reports (team_id, kind, period_key DESC);

-- Team-level Dialpad pulls the weekly/monthly reviews share (productivity per
-- agent from the per-user stats export + on-duty minutes; survey CSAT from
-- the csat export attributed through the calls records). One row per
-- (team, window); the same latch/resume shape as qa_eod_reports — request
-- ids are the resume handle, the ticker polls in bounded steps.
CREATE TABLE qa_sup_pulls (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id       TEXT NOT NULL REFERENCES teams(id),
    window_start  TEXT NOT NULL,          -- local YYYY-MM-DD inclusive
    window_end    TEXT NOT NULL,          -- local YYYY-MM-DD inclusive
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','fetching','ready','error')),
    export_ids    TEXT CHECK (export_ids IS NULL OR json_valid(export_ids)),
    data          TEXT CHECK (data IS NULL OR json_valid(data)),
    attempts      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    CONSTRAINT uq_sup_pulls UNIQUE (team_id, window_start, window_end)
);

-- §6 Action plan & accountability register: Issue → Cause → Action → Owner
-- → Follow-up → Result. Rows outlive the report that raised them so the
-- next review starts from last week's open items instead of a blank list.
CREATE TABLE qa_sup_action_plans (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id           TEXT NOT NULL REFERENCES teams(id),
    area              TEXT NOT NULL CHECK (area IN
                          ('abandon_rate','productivity','qa','csat','schedule',
                           'tickets','slack','training','attendance','other')),
    agent_id          INTEGER REFERENCES qa_agents(id),
    agent_name        TEXT,
    issue             TEXT NOT NULL CHECK (length(trim(issue)) > 0),
    cause             TEXT,
    action            TEXT NOT NULL CHECK (length(trim(action)) > 0),
    owner             TEXT NOT NULL,
    follow_up_date    TEXT,
    result            TEXT,
    status            TEXT NOT NULL DEFAULT 'open'
                      CHECK (status IN ('open','in_progress','done','dropped')),
    source_report_id  INTEGER REFERENCES qa_sup_reports(id),
    created_by        TEXT NOT NULL,
    created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    closed_at         TEXT
);
CREATE INDEX idx_sup_action_plans_open ON qa_sup_action_plans (team_id, status, follow_up_date);

-- Audit trail: who created / saved / submitted / published what, when.
CREATE TABLE qa_sup_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id    INTEGER REFERENCES qa_sup_reports(id) ON DELETE CASCADE,
    plan_id      INTEGER REFERENCES qa_sup_action_plans(id) ON DELETE CASCADE,
    action       TEXT NOT NULL,
    detail       TEXT CHECK (detail IS NULL OR json_valid(detail)),
    actor_email  TEXT NOT NULL,
    at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX idx_sup_events_report ON qa_sup_events (report_id, at);

-- Config rides teams.provider_config (house pattern: callcenter_id,
-- nightly_sweep, eod_sheet already live there). Targets that the Direction
-- index states are pre-filled (CSAT 4.1, two QA reviews per agent per
-- week); the ones it leaves to the team (abandon rate, productivity, QA
-- score) start NULL and the hub says "target not set" until a manager
-- saves them. Ticket categories and Slack subteams are the ShiftReport.md
-- §1.2/§1.3 vocabulary.
UPDATE teams SET provider_config = json_set(COALESCE(provider_config, '{}'), '$.deliverables', json('{
  "enabled": true,
  "timezone": "America/Mexico_City",
  "slack_channel": "C0664EX0SG3",
  "sheet_tab_prefix": "Supervisor",
  "meeting_weekday": 2,
  "targets": {
    "abandon_rate_pct": null,
    "sl_pct": 80,
    "productivity_per_hour": null,
    "qa_score": null,
    "csat": 4.1,
    "qa_reviews_per_agent_week": 2
  },
  "tickets": {
    "queue_id": 1,
    "categories": [
      {"label": "Maintenance", "type_id": 4},
      {"label": "I Need Something Else", "type_id": 34},
      {"label": "Packages", "reason_id": 16},
      {"label": "Lockouts", "reason_id": 7}
    ]
  },
  "slack_subteams": ["S046UTKHHUZ", "S066VLZGGJ0"],
  "attendance_channel": "C066HERTN81",
  "wfm_dashboard_url": "https://app.sigmacomputing.com/landing/workbook/Member-Support-Workforce-Management-5VfuiM7BRRTFrpOLvK9iOc?:nodeId=-UsAqHY65U",
  "wfm": {"status_table": null, "csat_table": null}
}')) WHERE id = 'member_support';

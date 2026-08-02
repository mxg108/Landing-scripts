-- ============================================================================
-- D1 migration 0001: consolidated schema (PortManifest.md §7)
--
-- Source of truth: database/migrations/004–018 (Railway Postgres), collapsed
-- to FINAL state — a fresh D1 replays no ALTER history. Sources are cited
-- per table as [NNN].
--
-- Translation rules applied (manifest §7):
--   schema prefix        public.X → X · qa.X → qa_X · command_center.X → cc_X
--   IDENTITY PK          → INTEGER PRIMARY KEY AUTOINCREMENT
--   TIMESTAMPTZ          → TEXT ISO-8601 UTC; DEFAULT NOW() →
--                          strftime('%Y-%m-%dT%H:%M:%fZ','now')
--   JSONB                → TEXT + json_valid() CHECK
--   BOOLEAN              → INTEGER 0/1 CHECK
--   NUMERIC(x,y)         → REAL (rounding is TS-side, parity-gated)
--   TEXT[]               → TEXT JSON array + json_valid() CHECK
--   partial/expression indexes and generated columns carry over (SQLite
--   supports all used here); index names preserved
--
-- Deliberate deviations:
--   · qa.v_monthly_scores (015) NOT created — its America/Los_Angeles month
--     bucketing needs IANA TZ conversion, which SQLite lacks; the rollup
--     moves to worker code (JS Intl), same as the rest of the stats port.
--   · Seeds: only structural config rows ship here (teams, tags [009]).
--     rubric_versions / formula_versions / teams operational JSON (010) are
--     DATA — they arrive via the Postgres-dump import pipeline.
--   · embeddings.* (007) and mass_notifications (001/002): out of scope.
--   · qa_events is NEW (manifest §6.1) — the D1-as-bus table behind SSE.
--   · items / workflow_runs: Sandy template tables (demo page + workflow
--     callback persistence). items drops when the first real page lands.
-- ============================================================================

-- ── teams [004 + 010 + 013] ─────────────────────────────────────────────────
CREATE TABLE teams (
    id                   TEXT PRIMARY KEY,
    name                 TEXT NOT NULL,
    timezone             TEXT NOT NULL,
    default_language     TEXT NOT NULL,
    active               INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
    -- [010] operational config absorbed from config/teams/<team>.json
    company              TEXT,
    stats_config         TEXT CHECK (stats_config IS NULL OR json_valid(stats_config)),
    gemini_config        TEXT CHECK (gemini_config IS NULL OR json_valid(gemini_config)),
    excluded_test_agents TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(excluded_test_agents)),
    sheets_config        TEXT CHECK (sheets_config IS NULL OR json_valid(sheets_config)),
    -- [013] scoring truth flag (kept for shadow-phase symmetry; D1 side is
    -- always effectively 'postgres'-mode code but the flag preserves parity
    -- with Railway rows during reconciliation)
    scoring_owner        TEXT NOT NULL DEFAULT 'postgres'
                         CHECK (scoring_owner IN ('sheets','postgres')),
    created_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

INSERT OR IGNORE INTO teams (id, name, timezone, default_language) VALUES
    ('member_support', 'Member Support', 'America/Mexico_City', 'en'),
    ('sales',          'Sales',          'America/Mexico_City', 'en');

-- ── qa_agents [006] ─────────────────────────────────────────────────────────
CREATE TABLE qa_agents (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id           TEXT NOT NULL REFERENCES teams(id),
    name              TEXT NOT NULL,
    canonical_name    TEXT,
    email             TEXT NOT NULL,
    supervisor_email  TEXT,
    dialpad_agent_id  TEXT,
    active            INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
    created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE UNIQUE INDEX uq_agents_team_lower_name ON qa_agents (team_id, lower(name));

-- ── qa_formula_versions [006] ───────────────────────────────────────────────
CREATE TABLE qa_formula_versions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    formula_version  TEXT NOT NULL UNIQUE,
    team_id          TEXT NOT NULL REFERENCES teams(id),
    formula_json     TEXT NOT NULL CHECK (json_valid(formula_json)),
    effective_from   TEXT NOT NULL,
    effective_until  TEXT,
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX idx_formula_versions_team_effective
    ON qa_formula_versions (team_id, effective_from DESC);

-- ── qa_rubric_versions [010] ────────────────────────────────────────────────
CREATE TABLE qa_rubric_versions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    rubric_version   TEXT NOT NULL UNIQUE,
    team_id          TEXT NOT NULL REFERENCES teams(id),
    rubric_json      TEXT NOT NULL CHECK (json_valid(rubric_json)),
    effective_from   TEXT NOT NULL,
    effective_until  TEXT,
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX idx_rubric_versions_team_effective
    ON qa_rubric_versions (team_id, effective_from DESC);

-- ── cc_webhook_events [005] ─────────────────────────────────────────────────
CREATE TABLE cc_webhook_events (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    team_id                  TEXT NOT NULL REFERENCES teams(id),
    event_kind               TEXT NOT NULL CHECK (event_kind IN ('call','agent_status')),
    dialpad_call_id          TEXT,
    dialpad_master_call_id   TEXT,
    dialpad_agent_id         TEXT,
    state                    TEXT NOT NULL CHECK (length(state) > 0),
    event_timestamp          TEXT NOT NULL,
    raw_payload              TEXT NOT NULL CHECK (json_valid(raw_payload)),
    processed_at             TEXT,
    CHECK (event_kind <> 'call' OR dialpad_call_id IS NOT NULL)
);
CREATE UNIQUE INDEX uq_webhook_call ON cc_webhook_events
    (dialpad_call_id, state, event_timestamp) WHERE event_kind = 'call';
CREATE UNIQUE INDEX uq_webhook_agent ON cc_webhook_events
    (dialpad_agent_id, state, event_timestamp) WHERE event_kind = 'agent_status';
CREATE INDEX idx_webhook_replay ON cc_webhook_events (team_id, event_timestamp, id);

-- ── cc_calls [005 + 016 + 017] ──────────────────────────────────────────────
CREATE TABLE cc_calls (
    id                           INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id                      TEXT NOT NULL REFERENCES teams(id),
    dialpad_call_id              TEXT NOT NULL,
    dialpad_master_call_id       TEXT,
    dialpad_entry_point_call_id  TEXT,
    started_at                   TEXT,
    rang_at                      TEXT,
    connected_at                 TEXT,
    ended_at                     TEXT,
    total_duration_ms            INTEGER,
    direction                    TEXT,
    external_number              TEXT,
    internal_number              TEXT,
    group_id                     TEXT,
    dialpad_agent_id             TEXT,
    agent_name                   TEXT,
    caller_name                  TEXT,
    caller_phone_e164            TEXT,
    caller_email                 TEXT,
    target_name                  TEXT,
    target_type                  TEXT,
    target_phone                 TEXT,
    mos_score                    REAL,
    was_recorded                 INTEGER CHECK (was_recorded IS NULL OR was_recorded IN (0,1)),
    is_transferred               INTEGER CHECK (is_transferred IS NULL OR is_transferred IN (0,1)),
    recording_urls               TEXT CHECK (recording_urls IS NULL OR json_valid(recording_urls)),
    last_state                   TEXT,
    last_state_at                TEXT,
    total_hold_seconds           INTEGER NOT NULL DEFAULT 0,
    raw_call_details             TEXT CHECK (raw_call_details IS NULL OR json_valid(raw_call_details)),
    scored                       INTEGER NOT NULL DEFAULT 0 CHECK (scored IN (0,1)),
    scored_at                    TEXT,
    evaluation_orphaned          INTEGER NOT NULL DEFAULT 0 CHECK (evaluation_orphaned IN (0,1)),
    evaluation_orphaned_at       TEXT,
    -- [017] widened vocabulary
    seen_via                     TEXT NOT NULL
        CHECK (seen_via IN ('webhook','qa_on_demand','qa_backfill','stats_pull')),
    first_seen_at                TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    last_updated_at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    -- [016] dispositions + AI CSAT
    disposition_category         TEXT,
    disposition                  TEXT,
    ai_csat                      REAL,
    disposition_source           TEXT
        CHECK (disposition_source IS NULL OR disposition_source IN ('webhook','stats_pull')),
    CHECK (scored = 0 OR scored_at IS NOT NULL),
    CHECK (evaluation_orphaned = 0 OR evaluation_orphaned_at IS NOT NULL),
    CHECK ((disposition_category IS NULL) = (disposition_source IS NULL)),
    CHECK (disposition IS NULL OR disposition_category IS NOT NULL),
    CONSTRAINT uq_calls_team_call_id UNIQUE (team_id, dialpad_call_id)
);
CREATE INDEX idx_calls_team_scored_connected ON cc_calls (team_id, scored, connected_at);
CREATE INDEX idx_calls_team_connected ON cc_calls (team_id, connected_at DESC)
    WHERE connected_at IS NOT NULL;
CREATE INDEX idx_calls_agent ON cc_calls (team_id, dialpad_agent_id)
    WHERE dialpad_agent_id IS NOT NULL;
CREATE INDEX idx_calls_entry_point_call_id ON cc_calls (team_id, dialpad_entry_point_call_id)
    WHERE dialpad_entry_point_call_id IS NOT NULL;
CREATE INDEX idx_calls_master_call_id ON cc_calls (team_id, dialpad_master_call_id)
    WHERE dialpad_master_call_id IS NOT NULL;

-- ── qa_evaluations [006 + 009 + 010 + 016 + 018] ────────────────────────────
CREATE TABLE qa_evaluations (
    id                            INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id                       TEXT NOT NULL REFERENCES teams(id),
    agent_id                      INTEGER REFERENCES qa_agents(id),
    agent_name_raw                TEXT NOT NULL,
    agent_email                   TEXT,
    evaluator_email               TEXT,
    state                         TEXT NOT NULL CHECK (state IN ('draft','approved','finalized')),
    source                        TEXT NOT NULL CHECK (source IN ('ai','manual','ai_reviewed')),
    call_connected_at             TEXT,
    call_started_at               TEXT,
    call_ended_at                 TEXT,
    call_duration_ms              INTEGER,
    call_type                     TEXT,
    language                      TEXT,
    dialpad_call_id               TEXT,
    dialpad_master_call_id        TEXT,
    dialpad_entry_point_call_id   TEXT,
    dialpad_link                  TEXT,
    command_center_call_id        INTEGER REFERENCES cc_calls(id),
    mos_score                     REAL,
    recording_urls                TEXT CHECK (recording_urls IS NULL OR json_valid(recording_urls)),
    dialpad_call_metadata         TEXT CHECK (dialpad_call_metadata IS NULL OR json_valid(dialpad_call_metadata)),
    caller_name                   TEXT,
    caller_phone                  TEXT,
    caller_email                  TEXT,
    call_summary                  TEXT,
    annotated_transcript          TEXT CHECK (annotated_transcript IS NULL OR json_valid(annotated_transcript)),
    key_strengths                 TEXT,
    opportunities                 TEXT,
    overall_score                 REAL,
    formula_version               TEXT REFERENCES qa_formula_versions(formula_version),
    models_used                   TEXT NOT NULL CHECK (json_valid(models_used)),
    ai_provider_primary           TEXT CHECK (ai_provider_primary IS NULL OR ai_provider_primary IN
                                      ('gemini','landgpt','landgpt_with_gemini_fallback')),
    estimated_cost_usd            REAL,
    csat_score                    REAL,
    sop_used_document_id          INTEGER,
    sampling_status               TEXT NOT NULL DEFAULT 'not_sampled',
    scoring_status                TEXT NOT NULL DEFAULT 'complete'
        CHECK (scoring_status IN ('complete','flagged_long_call','errored',
                                  'landgpt_unavailable_routed_to_gemini',
                                  'flagged_human_review')),          -- [009]
    created_at                    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    approved_at                   TEXT,
    finalized_at                  TEXT,
    -- [009] coaching + human-review columns
    needs_coaching                TEXT CHECK (needs_coaching IS NULL OR needs_coaching IN ('Y','N')),
    action_plan                   TEXT,
    human_review_required_at      TEXT,
    human_review_completed_at     TEXT,
    -- [010] rubric stamp
    rubric_version                TEXT REFERENCES qa_rubric_versions(rubric_version),
    -- [016] disposition stamps + Dialpad AI CSAT
    dialpad_disposition_category  TEXT,
    dialpad_disposition           TEXT,
    ai_csat                       REAL,
    -- [018] once-ever auto-rescore latch
    auto_rescored_at              TEXT,
    CHECK (state <> 'finalized' OR overall_score IS NOT NULL),
    CHECK (state = 'draft' OR (evaluator_email IS NOT NULL AND approved_at IS NOT NULL)),
    CHECK (state <> 'finalized' OR finalized_at IS NOT NULL),
    CHECK (human_review_completed_at IS NULL OR human_review_required_at IS NOT NULL)
);
CREATE UNIQUE INDEX uq_eval_team_call_id ON qa_evaluations (team_id, dialpad_call_id)
    WHERE dialpad_call_id IS NOT NULL;
CREATE UNIQUE INDEX uq_eval_team_link ON qa_evaluations (team_id, dialpad_link)
    WHERE dialpad_link IS NOT NULL;
CREATE INDEX idx_eval_team_time ON qa_evaluations (team_id, finalized_at)
    WHERE state = 'finalized';
CREATE INDEX idx_eval_agent_time ON qa_evaluations (agent_id, finalized_at)
    WHERE state = 'finalized' AND agent_id IS NOT NULL;
CREATE INDEX idx_eval_entry_point_call_id ON qa_evaluations (team_id, dialpad_entry_point_call_id)
    WHERE dialpad_entry_point_call_id IS NOT NULL;               -- [014]
CREATE INDEX idx_eval_human_review_queue ON qa_evaluations (team_id, human_review_required_at)
    WHERE state = 'draft' AND scoring_status = 'flagged_human_review';  -- [009]

-- ── qa_evaluation_sections [006 + 012] ──────────────────────────────────────
CREATE TABLE qa_evaluation_sections (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    evaluation_id  INTEGER NOT NULL REFERENCES qa_evaluations(id) ON DELETE CASCADE,
    section_id     TEXT NOT NULL,
    section_number INTEGER NOT NULL,
    score_type     TEXT NOT NULL CHECK (score_type IN
                       ('numeric','binary','manual_numeric','manual_binary','auto_value')),
    numeric_score  INTEGER CHECK (numeric_score IS NULL OR numeric_score BETWEEN 1 AND 5),
    binary_value   TEXT CHECK (binary_value IS NULL OR binary_value IN ('Y','N','NA')),
    score_source   TEXT NOT NULL CHECK (score_source IN ('ai','manual','auto_value','manual_default')),
    ai_provider    TEXT CHECK (ai_provider IS NULL OR ai_provider IN ('gemini','landgpt')),
    model          TEXT,
    confidence     TEXT,
    reasoning      TEXT,
    -- [012] widened value-matches-type (numeric rows may carry NA)
    CHECK (
        (score_type IN ('numeric','manual_numeric')
            AND numeric_score IS NOT NULL AND binary_value IS NULL)
        OR (score_type IN ('numeric','manual_numeric')
            AND numeric_score IS NULL AND binary_value = 'NA')
        OR (score_type IN ('binary','manual_binary')
            AND binary_value IS NOT NULL AND numeric_score IS NULL)
        OR (score_type = 'auto_value'
            AND ((numeric_score IS NOT NULL AND binary_value IS NULL)
              OR (binary_value IS NOT NULL AND numeric_score IS NULL)))
    ),
    CHECK ((score_source = 'ai' AND ai_provider IS NOT NULL)
        OR (score_source <> 'ai' AND ai_provider IS NULL)),
    CONSTRAINT uq_eval_sections_eval_section UNIQUE (evaluation_id, section_id)
);
CREATE INDEX idx_sections_trend ON qa_evaluation_sections (section_id, evaluation_id);

-- ── qa_formula_compliance_sweeps [006] ──────────────────────────────────────
CREATE TABLE qa_formula_compliance_sweeps (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    evaluation_id          INTEGER NOT NULL REFERENCES qa_evaluations(id) ON DELETE CASCADE,
    swept_formula_version  TEXT NOT NULL REFERENCES qa_formula_versions(formula_version),
    recomputed_score       REAL NOT NULL,
    original_score         REAL NOT NULL,
    delta                  REAL GENERATED ALWAYS AS (recomputed_score - original_score) STORED,
    epsilon                REAL NOT NULL DEFAULT 0.05,
    flagged                INTEGER NOT NULL CHECK (flagged IN (0,1)),
    swept_at               TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    CONSTRAINT uq_sweeps_eval_version UNIQUE (evaluation_id, swept_formula_version)
);
CREATE INDEX idx_sweeps_formula_flagged ON qa_formula_compliance_sweeps (swept_formula_version, flagged);
CREATE INDEX idx_sweeps_eval ON qa_formula_compliance_sweeps (evaluation_id);

-- ── qa_score_audit + archive [006 + 018 action vocab] ───────────────────────
CREATE TABLE qa_score_audit (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    api_key_role     TEXT NOT NULL,
    evaluator_email  TEXT,
    agent_email      TEXT,
    agent_name       TEXT,
    call_id          TEXT,
    target_team      TEXT,
    action           TEXT NOT NULL CHECK (action IN
        ('scored','denied','approved','evaluation_orphaned','rescored','overridden')),
    result_row       INTEGER,
    notes            TEXT
);
CREATE TABLE qa_score_audit_archive (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    original_id      INTEGER NOT NULL,
    timestamp        TEXT NOT NULL,
    api_key_role     TEXT NOT NULL,
    evaluator_email  TEXT,
    agent_email      TEXT,
    agent_name       TEXT,
    call_id          TEXT,
    target_team      TEXT,
    action           TEXT NOT NULL CHECK (action IN
        ('scored','denied','approved','evaluation_orphaned','rescored','overridden')),
    result_row       INTEGER,
    notes            TEXT,
    archived_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- ── qa_api_audit_log [006] ──────────────────────────────────────────────────
CREATE TABLE qa_api_audit_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    team_id             TEXT,
    endpoint            TEXT NOT NULL,
    method              TEXT NOT NULL,
    status_code         INTEGER,
    duration_ms         REAL,
    api_key_team        TEXT,
    api_key_role        TEXT,
    action              TEXT,
    model               TEXT,
    estimated_cost_usd  REAL,
    call_id             TEXT,
    agent_name          TEXT,
    error_detail        TEXT
);

-- ── qa_agent_stat_points [006] ──────────────────────────────────────────────
CREATE TABLE qa_agent_stat_points (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id         TEXT NOT NULL REFERENCES teams(id),
    agent_id        INTEGER NOT NULL REFERENCES qa_agents(id),
    evaluation_id   INTEGER NOT NULL UNIQUE REFERENCES qa_evaluations(id),
    score           REAL NOT NULL,
    ewma            REAL NOT NULL,
    ewma_lambda     REAL NOT NULL,
    spc_mean        REAL,
    spc_sigma       REAL,
    spc_flags       TEXT CHECK (spc_flags IS NULL OR json_valid(spc_flags)),
    coverage_regime TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX idx_stat_points_agent ON qa_agent_stat_points (agent_id, id);

-- ── qa_tags + qa_evaluation_tags [009] ──────────────────────────────────────
CREATE TABLE qa_tags (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    slug        TEXT NOT NULL UNIQUE,
    category    TEXT NOT NULL,
    label       TEXT NOT NULL,
    description TEXT,
    active      INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
INSERT OR IGNORE INTO qa_tags (slug, category, label, description) VALUES
    ('sop',         'human_review_focus', 'SOP',
     'Standard operating procedure / process compliance'),
    ('soft_skills', 'human_review_focus', 'Soft Skills',
     'Communication, empathy, tone, customer experience'),
    ('hard_skills', 'human_review_focus', 'Hard Skills',
     'Product knowledge, tool usage, technical execution'),
    ('efficiency',  'human_review_focus', 'Efficiency',
     'Call structure, hold time use, escalation appropriateness, resource leverage');
CREATE INDEX idx_tags_category_active ON qa_tags (category) WHERE active = 1;

CREATE TABLE qa_evaluation_tags (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    evaluation_id  INTEGER NOT NULL REFERENCES qa_evaluations(id) ON DELETE CASCADE,
    tag_id         INTEGER NOT NULL REFERENCES qa_tags(id),
    source         TEXT NOT NULL CHECK (source IN ('manager','ai','auto')),
    created_by     TEXT,
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    CONSTRAINT uq_evaluation_tags_eval_tag_source UNIQUE (evaluation_id, tag_id, source)
);
CREATE INDEX idx_evaluation_tags_eval ON qa_evaluation_tags (evaluation_id);
CREATE INDEX idx_evaluation_tags_tag_source ON qa_evaluation_tags (tag_id, source);

-- ── qa_coachings + qa_coaching_evaluations [009] ────────────────────────────
CREATE TABLE qa_coachings (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id              INTEGER NOT NULL REFERENCES qa_agents(id),
    team_id               TEXT NOT NULL REFERENCES teams(id),
    conducted_by_role     TEXT NOT NULL CHECK (conducted_by_role IN
                              ('team_lead','manager','hr','external')),
    conducted_by_email    TEXT,
    status                TEXT NOT NULL DEFAULT 'pending'
                          CHECK (status IN ('pending','completed','cancelled')),
    action_plan           TEXT,
    action_plan_deadline  TEXT,
    coaching_summary      TEXT,
    agent_attitude        TEXT CHECK (agent_attitude IS NULL OR agent_attitude IN
                              ('receptive','engaged','neutral','defensive','dismissive','mixed')),
    created_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    scheduled_at          TEXT,
    completed_at          TEXT,
    completed_by          TEXT,
    CHECK (status <> 'completed'
        OR (coaching_summary IS NOT NULL AND completed_at IS NOT NULL
            AND completed_by IS NOT NULL))
);
CREATE INDEX idx_coachings_pending ON qa_coachings (team_id, action_plan_deadline)
    WHERE status = 'pending';
CREATE INDEX idx_coachings_agent_status ON qa_coachings (agent_id, status, created_at DESC);

CREATE TABLE qa_coaching_evaluations (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    coaching_id             INTEGER NOT NULL REFERENCES qa_coachings(id) ON DELETE CASCADE,
    evaluation_id           INTEGER NOT NULL REFERENCES qa_evaluations(id),
    opportunities_snapshot  TEXT,
    per_eval_note           TEXT,
    linked_at               TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    CONSTRAINT uq_coaching_evals_coaching_eval UNIQUE (coaching_id, evaluation_id)
);
CREATE INDEX idx_coaching_evals_eval ON qa_coaching_evaluations (evaluation_id);

-- ── qa_assessments + qa_assessment_sections [011] ───────────────────────────
CREATE TABLE qa_assessments (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id              INTEGER NOT NULL REFERENCES qa_agents(id),
    team_id               TEXT NOT NULL REFERENCES teams(id),
    time_range_days       INTEGER NOT NULL CHECK (time_range_days > 0),
    range_start_at        TEXT NOT NULL,
    range_end_at          TEXT NOT NULL,
    evaluations_included  INTEGER NOT NULL CHECK (evaluations_included >= 0),
    overall_assessment    TEXT NOT NULL,
    rubric_version        TEXT NOT NULL REFERENCES qa_rubric_versions(rubric_version),
    formula_version       TEXT REFERENCES qa_formula_versions(formula_version),
    models_used           TEXT NOT NULL CHECK (json_valid(models_used)),
    estimated_cost_usd    REAL,
    generated_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    is_current            INTEGER NOT NULL DEFAULT 1 CHECK (is_current IN (0,1)),
    CHECK (range_end_at >= range_start_at)
);
CREATE INDEX idx_assessments_current
    ON qa_assessments (agent_id, time_range_days, generated_at DESC)
    WHERE is_current = 1;
CREATE INDEX idx_assessments_team_generated ON qa_assessments (team_id, generated_at DESC);

CREATE TABLE qa_assessment_sections (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    assessment_id   INTEGER NOT NULL REFERENCES qa_assessments(id) ON DELETE CASCADE,
    section_id      TEXT NOT NULL,
    section_name    TEXT NOT NULL,
    section_number  INTEGER NOT NULL,
    trend           TEXT NOT NULL CHECK (trend IN ('improving','stable','declining')),
    summary         TEXT NOT NULL,
    coaching_tip    TEXT NOT NULL,
    CONSTRAINT uq_assessment_sections_assessment_section UNIQUE (assessment_id, section_id)
);
CREATE INDEX idx_assessment_sections_assessment ON qa_assessment_sections (assessment_id);
CREATE INDEX idx_assessment_sections_section_trend ON qa_assessment_sections (section_id, trend);

-- ── cc_chiclets + cc_chiclet_events [005] ───────────────────────────────────
CREATE TABLE cc_chiclets (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id           TEXT NOT NULL REFERENCES teams(id),
    type              TEXT NOT NULL CHECK (type IN
                          ('hold','repeated','frequent','qa_outlier',
                           'sheets_update','mass_notif','profanity')),
    tier              TEXT NOT NULL CHECK (tier IN ('T1','T2','T3')),
    status            TEXT NOT NULL CHECK (status IN ('active','resolved')),
    border_state      TEXT,
    source_event_id   INTEGER REFERENCES cc_webhook_events(id),
    caller_phone_e164 TEXT,
    agent_name        TEXT,
    summary           TEXT NOT NULL,
    data              TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(data)),
    created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    resolved_at       TEXT,
    resolved_by       TEXT,
    CHECK (status <> 'resolved' OR (resolved_at IS NOT NULL AND resolved_by IS NOT NULL))
);
CREATE TABLE cc_chiclet_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chiclet_id  INTEGER NOT NULL REFERENCES cc_chiclets(id) ON DELETE CASCADE,
    event_type  TEXT NOT NULL CHECK (event_type IN ('created','updated','escalated','resolved')),
    payload     TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(payload)),
    emitted_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- ── cc_frequent_callers_cache [005] ─────────────────────────────────────────
CREATE TABLE cc_frequent_callers_cache (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id           TEXT NOT NULL REFERENCES teams(id),
    caller_phone_e164 TEXT NOT NULL,
    caller_name       TEXT,
    unit              TEXT,
    category          TEXT,
    flag_reason       TEXT,
    last_call_at      TEXT,
    total_calls_30d   INTEGER,
    snapshot_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    snapshot_id       INTEGER NOT NULL
);
CREATE INDEX idx_frequent_callers_team_phone
    ON cc_frequent_callers_cache (team_id, caller_phone_e164);

-- ── cc_dialpad_agents [005] ─────────────────────────────────────────────────
CREATE TABLE cc_dialpad_agents (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id           TEXT NOT NULL REFERENCES teams(id),
    dialpad_agent_id  TEXT NOT NULL,
    display_name      TEXT NOT NULL,
    email             TEXT,
    first_seen_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    last_seen_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    CONSTRAINT uq_dialpad_agents_team_agent UNIQUE (team_id, dialpad_agent_id)
);

-- ── cc_hold_intervals [016] ─────────────────────────────────────────────────
CREATE TABLE cc_hold_intervals (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id          TEXT NOT NULL REFERENCES teams(id),
    dialpad_call_id  TEXT NOT NULL,
    call_id          INTEGER NOT NULL REFERENCES cc_calls(id) ON DELETE CASCADE,
    started_at       TEXT NOT NULL,
    ended_at         TEXT NOT NULL,
    seconds          INTEGER NOT NULL CHECK (seconds >= 0),
    ended_by         TEXT NOT NULL CHECK (ended_by IN ('connected','hangup')),
    CHECK (ended_at >= started_at)
);
CREATE INDEX idx_hold_intervals_call_id ON cc_hold_intervals (call_id);
CREATE INDEX idx_hold_intervals_team_call ON cc_hold_intervals (team_id, dialpad_call_id);

-- ── qa_events [NEW — manifest §6.1, SSE bus] ────────────────────────────────
-- The D1-as-bus table behind /events/stream. Publishers INSERT in the same
-- transaction as the action (eval approve → 'eval_approved'; agent-status
-- ingestion → 'agent_status'). Daily cron prunes rows older than 7 days.
CREATE TABLE qa_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id     TEXT NOT NULL REFERENCES teams(id),
    type        TEXT NOT NULL CHECK (length(type) > 0),
    payload     TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(payload)),
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX idx_qa_events_team_id ON qa_events (team_id, id);

-- ── v_history_long [015] ────────────────────────────────────────────────────
-- (v_monthly_scores deliberately not ported — see header.)
CREATE VIEW qa_v_history_long AS
SELECT e.team_id,
       e.id AS evaluation_id,
       e.agent_id,
       e.agent_name_raw,
       COALESCE(e.call_connected_at, e.approved_at) AS ts,
       e.approved_at,
       e.overall_score,
       e.evaluator_email,
       es.section_id,
       es.section_number,
       es.score_type,
       es.numeric_score,
       es.binary_value
FROM qa_evaluations e
JOIN qa_evaluation_sections es ON es.evaluation_id = e.id
WHERE e.state = 'finalized';

-- ── Sandy template tables ───────────────────────────────────────────────────
-- workflow_runs: callback persistence (template contract — workers are
-- stateless; callback results must land in D1). items: template demo page,
-- drops when the first real page lands.
CREATE TABLE IF NOT EXISTS workflow_runs (
    run_id         TEXT PRIMARY KEY,
    workflow_name  TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'pending',
    result         TEXT,
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS items (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

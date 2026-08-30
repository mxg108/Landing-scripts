-- 0007_coaching_loop.sql — CoachingLoopSpec §3 (CL1).
-- The coaching loop: post-deadline outcome verification on qa_coachings,
-- structured commitments, persisted insight narratives, the confirm-queue
-- index, the cc_chiclets 'coaching' type widening (§6.5), and the
-- qa_coaching_evaluations rebuild without its evaluation_id FK (§3 —
-- cross-ownership links must survive the shadow sync's wipe-then-reimport
-- in separate transactions; shadow_sync scopes the junction wipe by
-- coaching_id from CL1 on).

-- ── qa_coachings: outcome stamps (derived from commitment verdicts at
--    confirm time; stored for cheap one-pager/insights reads) ─────────────
ALTER TABLE qa_coachings ADD COLUMN outcome TEXT
    CHECK (outcome IS NULL OR outcome IN ('met','partially_met','not_met'));
ALTER TABLE qa_coachings ADD COLUMN outcome_confirmed_by TEXT;
ALTER TABLE qa_coachings ADD COLUMN outcome_confirmed_at TEXT;
ALTER TABLE qa_coachings ADD COLUMN outcome_note TEXT;

-- F4 queue predicate: conducted sessions awaiting confirmation, by deadline.
CREATE INDEX idx_coachings_confirm_queue
    ON qa_coachings (team_id, action_plan_deadline)
    WHERE status = 'completed' AND outcome IS NULL;

-- ── qa_coaching_commitments (Sandy-only: no Railway counterpart, never
--    in shadow_sync WIPE/RESYNC; plain AUTOINCREMENT ids are fine) ────────
CREATE TABLE qa_coaching_commitments (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    coaching_id        INTEGER NOT NULL REFERENCES qa_coachings(id) ON DELETE CASCADE,
    commitment         TEXT NOT NULL CHECK (length(trim(commitment)) > 0),
    section_id         TEXT,
    status             TEXT NOT NULL DEFAULT 'open'
                       CHECK (status IN ('open','met','partially_met','not_met','waived')),
    confirmed_by       TEXT,
    confirmed_at       TEXT,
    confirmation_note  TEXT,
    created_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    CHECK (status = 'open' OR (confirmed_by IS NOT NULL AND confirmed_at IS NOT NULL))
);
CREATE INDEX idx_coaching_commitments_coaching
    ON qa_coaching_commitments (coaching_id);

-- ── qa_coaching_insights (Sandy-only; §8 scopes coaching|team — agent
--    progression assessments reuse qa_assessments at high-range ids) ──────
CREATE TABLE qa_coaching_insights (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    scope               TEXT NOT NULL CHECK (scope IN ('coaching','team')),
    team_id             TEXT NOT NULL REFERENCES teams(id),
    agent_id            INTEGER,
    coaching_id         INTEGER,
    window_start        TEXT,
    window_end          TEXT,
    facts               TEXT NOT NULL CHECK (json_valid(facts)),
    narrative           TEXT NOT NULL,
    models_used         TEXT NOT NULL CHECK (json_valid(models_used)),
    estimated_cost_usd  REAL,
    generated_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    is_current          INTEGER NOT NULL DEFAULT 1 CHECK (is_current IN (0,1))
);
CREATE INDEX idx_coaching_insights_current
    ON qa_coaching_insights (team_id, scope, generated_at DESC)
    WHERE is_current = 1;

-- ── qa_coaching_evaluations: rebuild WITHOUT the evaluation_id FK ────────
-- The builder links Sandy-born coachings to Railway-born evals; a
-- cross-ownership FK cannot survive the sync's wipe commit (the CL0 bug
-- class), whichever way the junction wipe is scoped. evaluation_id becomes
-- a soft reference; app code owns integrity (deleteEvaluation keeps its
-- explicit unlink). agent_id/coaching_id soft refs on the insights table
-- above are soft for the same reason. Nothing references this table, so
-- the rebuild is self-contained.
CREATE TABLE qa_coaching_evaluations_new (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    coaching_id             INTEGER NOT NULL REFERENCES qa_coachings(id) ON DELETE CASCADE,
    evaluation_id           INTEGER NOT NULL,
    opportunities_snapshot  TEXT,
    per_eval_note           TEXT,
    linked_at               TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    CONSTRAINT uq_coaching_evals_coaching_eval UNIQUE (coaching_id, evaluation_id)
);
INSERT INTO qa_coaching_evaluations_new
    (id, coaching_id, evaluation_id, opportunities_snapshot, per_eval_note, linked_at)
    SELECT id, coaching_id, evaluation_id, opportunities_snapshot, per_eval_note, linked_at
    FROM qa_coaching_evaluations;
DROP TABLE qa_coaching_evaluations;
ALTER TABLE qa_coaching_evaluations_new RENAME TO qa_coaching_evaluations;
CREATE INDEX idx_coaching_evals_eval ON qa_coaching_evaluations (evaluation_id);

-- ── cc_chiclets: widen the type enum with 'coaching' (§6.5) ──────────────
-- Both chiclet tables verified EMPTY in live D1 (2026-08-16) and are
-- Sandy-owned (outside the cc incremental sync). Copies kept anyway so the
-- migration is correct on any database state. RENAME rewrites the
-- referencing FK clause on cc_chiclet_events_new (modern SQLite).
CREATE TABLE cc_chiclets_new (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id           TEXT NOT NULL REFERENCES teams(id),
    type              TEXT NOT NULL CHECK (type IN
                          ('hold','repeated','frequent','qa_outlier',
                           'sheets_update','mass_notif','profanity','coaching')),
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
INSERT INTO cc_chiclets_new
    (id, team_id, type, tier, status, border_state, source_event_id,
     caller_phone_e164, agent_name, summary, data, created_at, resolved_at, resolved_by)
    SELECT id, team_id, type, tier, status, border_state, source_event_id,
           caller_phone_e164, agent_name, summary, data, created_at, resolved_at, resolved_by
    FROM cc_chiclets;
CREATE TABLE cc_chiclet_events_new (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chiclet_id  INTEGER NOT NULL REFERENCES cc_chiclets_new(id) ON DELETE CASCADE,
    event_type  TEXT NOT NULL CHECK (event_type IN ('created','updated','escalated','resolved')),
    payload     TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(payload)),
    emitted_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
INSERT INTO cc_chiclet_events_new (id, chiclet_id, event_type, payload, emitted_at)
    SELECT id, chiclet_id, event_type, payload, emitted_at FROM cc_chiclet_events;
DROP TABLE cc_chiclet_events;
DROP TABLE cc_chiclets;
ALTER TABLE cc_chiclets_new RENAME TO cc_chiclets;
ALTER TABLE cc_chiclet_events_new RENAME TO cc_chiclet_events;

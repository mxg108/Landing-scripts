-- ============================================================================
-- Migration 009: VP review additions (2026-06-24 Ops VP review)
-- Spec: database/SQLMigration.md §3.4 (column additions),
--       §3.14 (pipeline trigger), §3.15–§3.18 (tags + coachings tables),
--       §7.6 (migration ordering), §8.6 (auto-tagging forward path)
--
-- Three coupled concerns:
--
--   1) Coaching workflow — qa.coachings (one row per session) + M:N
--      qa.coaching_evaluations. Escalation pattern TL → Manager → HR can
--      revisit the same eval; one session can cover multiple evals.
--
--   2) Tag taxonomy — qa.tags (slug + category) + qa.evaluation_tags
--      (M:N with source provenance). Seeds 4 human-review-focus tags.
--      Forward-compatible with LandGPT v2 auto-tagging (source='ai').
--
--   3) Pipeline trigger — new 'flagged_human_review' value on
--      qa.evaluations.scoring_status + paired timestamps. The trigger
--      condition lives in per-team overall_formula.json
--      (human_review_triggers array), not the schema.
--
-- All ALTERs and CREATEs are reversible via the companion _down.sql.
-- This migration depends on 006 (qa.* base tables) being applied.
-- ============================================================================


-- ── (a) ALTER qa.evaluations — new columns + extended CHECK ─────────────────

ALTER TABLE qa.evaluations
    ADD COLUMN needs_coaching             TEXT,
    ADD COLUMN action_plan                TEXT,
    ADD COLUMN human_review_required_at   TIMESTAMPTZ,
    ADD COLUMN human_review_completed_at  TIMESTAMPTZ;

-- needs_coaching is Y/N or NULL (NULL = manager hasn't decided / not asked).
ALTER TABLE qa.evaluations
    ADD CONSTRAINT evaluations_needs_coaching_check
    CHECK (needs_coaching IS NULL OR needs_coaching IN ('Y', 'N'));

-- Pair invariant: you can't complete a review that was never required.
ALTER TABLE qa.evaluations
    ADD CONSTRAINT evaluations_human_review_pair_check
    CHECK (human_review_completed_at IS NULL
           OR human_review_required_at IS NOT NULL);

-- Extend scoring_status to admit the new 'flagged_human_review' value.
-- The existing constraint must be dropped and re-added since CHECK
-- definitions are not mutable in place. Any row with the new value gets
-- rejected by the OLD constraint, so this DDL is safe: no row can yet
-- carry the new value at apply time.
ALTER TABLE qa.evaluations
    DROP CONSTRAINT evaluations_scoring_status_check;

ALTER TABLE qa.evaluations
    ADD CONSTRAINT evaluations_scoring_status_check
    CHECK (scoring_status IN ('complete', 'flagged_long_call', 'errored',
                              'landgpt_unavailable_routed_to_gemini',
                              'flagged_human_review'));


-- ── (b) qa.tags — controlled tag vocabulary (§3.15) ─────────────────────────

CREATE TABLE qa.tags (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    slug        TEXT NOT NULL UNIQUE,
    -- Drives WHERE category = ... analytics. Initial human_review_focus
    -- namespace + future categories (compliance, soft_skills, operational,
    -- product, outcome) ship as rows, not migrations. No CHECK on the
    -- value space — keeping it open so adding 'product' next quarter is
    -- a row insert, not a migration.
    category    TEXT NOT NULL,
    label       TEXT NOT NULL,
    description TEXT,
    -- Soft-delete: managers can stop seeing a tag in dropdowns without
    -- losing historical references on qa.evaluation_tags.
    active      BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Seed: 4 initial human-review-focus tags per §3.15. ON CONFLICT keeps
-- the seed idempotent under re-run (the migration runner's transaction
-- wrapping makes this redundant, but matches 004's pattern).
INSERT INTO qa.tags (slug, category, label, description) VALUES
    ('sop',          'human_review_focus', 'SOP',
     'Standard operating procedure / process compliance'),
    ('soft_skills',  'human_review_focus', 'Soft Skills',
     'Communication, empathy, tone, customer experience'),
    ('hard_skills',  'human_review_focus', 'Hard Skills',
     'Product knowledge, tool usage, technical execution'),
    ('efficiency',   'human_review_focus', 'Efficiency',
     'Call structure, hold time use, escalation appropriateness, resource leverage')
ON CONFLICT (slug) DO NOTHING;


-- ── (c) qa.evaluation_tags — M:N join with provenance (§3.16) ───────────────

CREATE TABLE qa.evaluation_tags (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    evaluation_id  BIGINT NOT NULL REFERENCES qa.evaluations(id) ON DELETE CASCADE,
    tag_id         BIGINT NOT NULL REFERENCES qa.tags(id),
    -- 'manager' is the only source today; 'ai' lands with LandGPT v2's
    -- annotated-transcript path; 'auto' reserved for rule-based tagging
    -- (e.g. profanity detector → compliance.profanity_agent).
    source         TEXT NOT NULL,
    -- Email when source='manager'; NULL for ai/auto.
    created_by     TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT evaluation_tags_source_check
        CHECK (source IN ('manager', 'ai', 'auto')),
    -- The same tag from manager + ai coexist as distinct rows: agreement
    -- between the two is information ("AI saw this and manager confirmed").
    CONSTRAINT uq_evaluation_tags_eval_tag_source
        UNIQUE (evaluation_id, tag_id, source)
);


-- ── (d) qa.coachings — coaching session records (§3.17) ─────────────────────

CREATE TABLE qa.coachings (
    id                    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    agent_id              BIGINT NOT NULL REFERENCES qa.agents(id),
    team_id               TEXT NOT NULL REFERENCES public.teams(id),
    -- Escalation level derived from the role enum.
    conducted_by_role     TEXT NOT NULL,
    -- Free text email — not FK'd to a roles table (no roles table exists).
    conducted_by_email    TEXT,
    status                TEXT NOT NULL DEFAULT 'pending',
    -- The plan agreed in this session; supersedes qa.evaluations.action_plan
    -- (the evaluator's initial proposal) once agreed.
    action_plan           TEXT,
    action_plan_deadline  TIMESTAMPTZ,
    -- What was actually discussed; required at completion.
    coaching_summary      TEXT,
    agent_attitude        TEXT,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- When the session is set to happen — often populated at create time.
    scheduled_at          TIMESTAMPTZ,
    completed_at          TIMESTAMPTZ,
    completed_by          TEXT,
    CONSTRAINT coachings_conducted_by_role_check
        CHECK (conducted_by_role IN ('team_lead', 'manager', 'hr', 'external')),
    CONSTRAINT coachings_status_check
        CHECK (status IN ('pending', 'completed', 'cancelled')),
    CONSTRAINT coachings_agent_attitude_check
        CHECK (agent_attitude IS NULL OR agent_attitude IN
               ('receptive', 'engaged', 'neutral',
                'defensive', 'dismissive', 'mixed')),
    -- Completion pair: status='completed' MUST carry summary +
    -- completed_at + completed_by.
    CONSTRAINT coachings_completion_pair_check CHECK (
        status <> 'completed'
        OR (coaching_summary IS NOT NULL
            AND completed_at IS NOT NULL
            AND completed_by IS NOT NULL)
    )
);


-- ── (e) qa.coaching_evaluations — M:N coachings ↔ evaluations (§3.18) ──────

CREATE TABLE qa.coaching_evaluations (
    id                      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- Parent: deleting a coaching wipes its links to evals.
    coaching_id             BIGINT NOT NULL
                            REFERENCES qa.coachings(id) ON DELETE CASCADE,
    -- Child: deleting an eval should NOT silently strip it from
    -- historical coaching records. FK without CASCADE means an eval
    -- delete fails loudly if any coaching still references it; admin
    -- delete path explicitly handles the cleanup.
    evaluation_id           BIGINT NOT NULL REFERENCES qa.evaluations(id),
    opportunities_snapshot  TEXT,
    per_eval_note           TEXT,
    linked_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_coaching_evals_coaching_eval
        UNIQUE (coaching_id, evaluation_id)
);


-- ── (f) Indexes — partial + analytical lookups ─────────────────────────────
-- Per §3.14, §3.15, §3.16, §3.17, §3.18 + spec convention that "structural"
-- indexes ship with their tables (analytics indexes are in 008's scope but
-- for this incremental migration the analytics ones live here too — there
-- is no "010_indexes.sql" to split into).

-- Human-review queue: surfaces evals that flagged at Stage 1 (or Stage 1.5
-- re-evaluation) and await a reviewer. Partial — only draft + flagged rows
-- matter.
CREATE INDEX idx_eval_human_review_queue
    ON qa.evaluations (team_id, human_review_required_at)
    WHERE state = 'draft'
      AND scoring_status = 'flagged_human_review';

-- Tag analytics + dropdown lookups.
CREATE INDEX idx_tags_category_active
    ON qa.tags (category)
    WHERE active = TRUE;

-- "What tags does this eval have" — the most common read pattern.
CREATE INDEX idx_evaluation_tags_eval
    ON qa.evaluation_tags (evaluation_id);

-- "Which evals got tagged X by which source" — drives the future LandGPT-
-- vs-manager agreement analytics.
CREATE INDEX idx_evaluation_tags_tag_source
    ON qa.evaluation_tags (tag_id, source);

-- Coachings pending overdue-deadline surface.
CREATE INDEX idx_coachings_pending
    ON qa.coachings (team_id, action_plan_deadline)
    WHERE status = 'pending';

-- Per-agent coaching history view.
CREATE INDEX idx_coachings_agent_status
    ON qa.coachings (agent_id, status, created_at DESC);

-- "This call has been coached on N times" badge per §3.18.
CREATE INDEX idx_coaching_evals_eval
    ON qa.coaching_evaluations (evaluation_id);

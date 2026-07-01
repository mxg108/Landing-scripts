-- ============================================================================
-- Migration 011: qa.assessments + qa.assessment_sections (v1.4)
-- Spec: database/SQLMigration.md §3.20 (qa.assessments — append-only,
--       immutable AI artifacts), §3.21 (qa.assessment_sections —
--       normalized per-section), §7.6 (migration ordering)
--
-- Persists the frontend's AI Progression Assessment output past the
-- previous 1h in-memory TTL:
--
--   qa.assessments
--     Append-only history keyed by (agent_id, time_range_days,
--     generated_at). `is_current` flag identifies the authoritative row
--     for a given (agent, window) tuple. Stamps `rubric_version` +
--     `formula_version` for reproducibility (Q5.a).
--
--   qa.assessment_sections
--     One row per section per assessment. `section_name` + `section_number`
--     snapshotted at generation time so historical assessments render
--     correctly even after the rubric is reshaped (Q3.a).
--
-- IMMUTABILITY BY CONVENTION (Q4.a, load-bearing HR concern):
--   Ops VP directive — assessments are pure unadulterated AI output.
--   Application code and API surface (Wave 2) MUST NOT expose any
--   endpoint that UPDATEs assessment content columns. The only mutation
--   the write path performs is flipping the prior row's `is_current` to
--   FALSE when a successor is generated, which does not touch content.
--   This preserves clean HR separation during agent appeals — no
--   manager can be accused of tampering with the AI's output. Manager
--   commentary lives in qa.coachings.coaching_summary (§3.17), never
--   on qa.assessments.
--
-- Depends on 010 (qa.rubric_versions FK target).
-- Reversible via 011_assessments_down.sql.
-- ============================================================================


-- ── (a) qa.assessments — one row per generated assessment ──────────────────

CREATE TABLE qa.assessments (
    id                    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    agent_id              BIGINT NOT NULL REFERENCES qa.agents(id),
    team_id               TEXT NOT NULL REFERENCES public.teams(id),
    -- Flexible time window in days. 7w=49, 30, 60, 90 — future values
    -- (120d, 180d, etc.) require no schema change.
    time_range_days       INTEGER NOT NULL,
    range_start_at        TIMESTAMPTZ NOT NULL,
    range_end_at          TIMESTAMPTZ NOT NULL,
    -- How many qa.evaluations rows went into this assessment. Detailed
    -- eval list is reconstructable from qa.evaluations by agent_id +
    -- state='finalized' + finalized_at BETWEEN range_start_at AND
    -- range_end_at — no join table needed.
    evaluations_included  INTEGER NOT NULL,
    overall_assessment    TEXT NOT NULL,
    -- Reproducibility stamps per Q5.a. rubric_version is required
    -- because sections always reference an archived rubric.
    -- formula_version is nullable for edge cases (earliest evaluations
    -- before formula_versions has any row for the team).
    rubric_version        TEXT NOT NULL REFERENCES qa.rubric_versions(rubric_version),
    formula_version       TEXT REFERENCES qa.formula_versions(formula_version),
    -- Cascade provenance, same shape as qa.evaluations.models_used
    -- (§8.1). E.g. {"text": {"provider": "gemini", "model": "gemini-2.5-flash"}}.
    models_used           JSONB NOT NULL,
    -- Cloud API cost. Small per-assessment ($0.01-0.05) but visible
    -- across team volume in cost dashboards.
    estimated_cost_usd    NUMERIC(8,4),
    generated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- Load-bearing flag for the read path. The only column the write
    -- path is allowed to UPDATE (flipping FALSE on a prior row when a
    -- successor is generated for the same (agent_id, time_range_days)).
    -- No column carrying assessment CONTENT may ever be updated per Q4.a.
    is_current            BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT assessments_time_range_positive
        CHECK (time_range_days > 0),
    CONSTRAINT assessments_range_ordered
        CHECK (range_end_at >= range_start_at),
    CONSTRAINT assessments_evaluations_included_non_negative
        CHECK (evaluations_included >= 0)
);


-- ── (b) qa.assessment_sections — normalized per-section (§3.21) ────────────

CREATE TABLE qa.assessment_sections (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    assessment_id   BIGINT NOT NULL REFERENCES qa.assessments(id) ON DELETE CASCADE,
    -- Matches qa.evaluation_sections.section_id at generation time.
    -- No FK to a rubric_versions.sections row since sections live inside
    -- rubric_json JSONB. Consistency validated application-side against
    -- the assessment's rubric_version.
    section_id      TEXT NOT NULL,
    -- Snapshotted at generation time — preserves render fidelity if a
    -- section is later renamed. See §3.21 snapshot rationale.
    section_name    TEXT NOT NULL,
    section_number  SMALLINT NOT NULL,
    trend           TEXT NOT NULL,
    summary         TEXT NOT NULL,
    coaching_tip    TEXT NOT NULL,
    CONSTRAINT assessment_sections_trend_check
        CHECK (trend IN ('improving', 'stable', 'declining')),
    CONSTRAINT uq_assessment_sections_assessment_section
        UNIQUE (assessment_id, section_id)
);


-- ── (c) Indexes (§3.20.3 + §3.21) ──────────────────────────────────────────

-- The hot read path: "current assessment for this agent + window,
-- newest first" — partial WHERE is_current = TRUE keeps the index
-- narrow (only 1 current row per agent+window at steady state).
CREATE INDEX idx_assessments_current
    ON qa.assessments (agent_id, time_range_days, generated_at DESC)
    WHERE is_current = TRUE;

-- Team-wide "assessments generated this week" listing.
CREATE INDEX idx_assessments_team_generated
    ON qa.assessments (team_id, generated_at DESC);

-- CASCADE delete performance + per-assessment section lookup. Postgres
-- does not auto-index FK columns; explicit index makes the CASCADE
-- (and the standard "get all sections for this assessment" query) fast.
CREATE INDEX idx_assessment_sections_assessment
    ON qa.assessment_sections (assessment_id);

-- Cross-agent trend analytics: "find all agents with declining
-- process_adherence this quarter." Ops has been asking for this cut.
CREATE INDEX idx_assessment_sections_section_trend
    ON qa.assessment_sections (section_id, trend);

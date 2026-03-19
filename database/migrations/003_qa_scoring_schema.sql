-- ============================================================================
-- Migration 003: qa_scoring schema
-- Database: landing_member_support_db (Railway Postgres)
--
-- This schema stores QA evaluations for call center agents. It serves
-- both the existing manual QA flow (Google Form + Apps Script) and the
-- planned AI-Scoring pipeline (Phase 1+).
--
-- Tables:
--   agents           Agent directory (source of truth for name/email)
--   evaluations      One row per QA evaluation (manual or AI)
--   evaluation_scores  One row per scored section per evaluation
--
-- Design principles:
--   - Store call metadata from day one (enables stratified sampling later)
--   - Documentation (section 9) is always manual — stored but never AI-scored
--   - Overall score is stored (not recalculated) for historical accuracy
--   - Agent history queries must be fast (indexed by agent + timestamp)
-- ============================================================================

CREATE SCHEMA IF NOT EXISTS qa_scoring;

-- ── agents ───────────────────────────────────────────────────────────────────
-- Source of truth for agent identity. Maps to the "Mails" sheet today.
-- Agents can change teams/managers over time, so manager is stored
-- per-evaluation, not per-agent.

CREATE TABLE qa_scoring.agents (
    id              SERIAL PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    email           TEXT NOT NULL,
    active          BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── evaluations ──────────────────────────────────────────────────────────────
-- One row per QA evaluation. Captures everything about a single scored call.
-- Supports both manual (Google Form) and AI-generated evaluations.
--
-- The overall_score is stored as-is (not recalculated on read) so that
-- historical scores remain stable even if the formula changes.

CREATE TABLE qa_scoring.evaluations (
    id                  SERIAL PRIMARY KEY,

    -- Who was evaluated
    agent_id            INTEGER NOT NULL
                        REFERENCES qa_scoring.agents(id),

    -- Who performed the evaluation
    evaluator_email     TEXT NOT NULL,

    -- Source of this evaluation
    source              TEXT NOT NULL DEFAULT 'manual',
    -- 'manual' = Google Form, 'ai' = Gemini pipeline, 'ai_reviewed' = AI + manager approval

    -- Call metadata (available from Dialpad, stored from day one for sampling)
    call_date           TIMESTAMPTZ,
    call_duration_ms    INTEGER,
    call_type           TEXT,
    language            TEXT DEFAULT 'en',
    dialpad_call_id     TEXT,
    dialpad_link        TEXT,
    csat_score          NUMERIC(3,1),

    -- Sampling and workflow status (Phase 4)
    sampling_status     TEXT DEFAULT 'not_sampled',
    scoring_status      TEXT DEFAULT 'complete',
    flagged_long_call   BOOLEAN DEFAULT FALSE,

    -- Overall score (0-100 scale, calculated from numeric sections)
    overall_score       NUMERIC(5,1) NOT NULL,

    -- Qualitative feedback
    key_strengths       TEXT,
    improvements        TEXT,

    -- Audit
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── evaluation_scores ────────────────────────────────────────────────────────
-- One row per scored section per evaluation. Normalized so we can:
--   - Query trends per category across time
--   - Add new sections without schema changes
--   - Store AI confidence and reasoning per section
--
-- section_id values:
--   Numeric (1-5): greeting, call_purpose, match_moment, process_adherence,
--                   call_resolution, communication, efficiency, documentation
--   Binary (Y/N):  identity_validation, customer_resolution

CREATE TABLE qa_scoring.evaluation_scores (
    id              SERIAL PRIMARY KEY,

    evaluation_id   INTEGER NOT NULL
                    REFERENCES qa_scoring.evaluations(id)
                    ON DELETE CASCADE,

    -- Section identifier (e.g., 'greeting', 'call_purpose', 'identity_validation')
    section_id      TEXT NOT NULL,

    -- Score type determines which value column is populated
    score_type      TEXT NOT NULL DEFAULT 'numeric',
    -- 'numeric' = use numeric_score, 'binary' = use binary_value

    -- Numeric score (1-5 scale, NULL for binary sections)
    numeric_score   SMALLINT,

    -- Binary value (Y/N/NA, NULL for numeric sections)
    binary_value    TEXT,

    -- AI-specific fields (NULL for manual evaluations)
    confidence      TEXT,
    reasoning       TEXT,

    CONSTRAINT valid_score_type CHECK (score_type IN ('numeric', 'binary')),
    CONSTRAINT valid_numeric CHECK (numeric_score IS NULL OR numeric_score BETWEEN 1 AND 5),
    CONSTRAINT valid_binary CHECK (binary_value IS NULL OR binary_value IN ('Y', 'N', 'NA')),
    CONSTRAINT one_score_populated CHECK (
        (score_type = 'numeric' AND numeric_score IS NOT NULL AND binary_value IS NULL) OR
        (score_type = 'binary' AND binary_value IS NOT NULL AND numeric_score IS NULL)
    ),
    -- No duplicate sections per evaluation
    UNIQUE (evaluation_id, section_id)
);


-- ── Indexes ──────────────────────────────────────────────────────────────────

-- Agent history: last N evaluations for an agent, newest first
-- This is the primary query pattern (ProgressionCard, "history per agent")
CREATE INDEX idx_evaluations_agent_date
    ON qa_scoring.evaluations (agent_id, created_at DESC);

-- Lookup agent by name (case-insensitive)
CREATE INDEX idx_agents_name_lower
    ON qa_scoring.agents (LOWER(name));

-- Lookup agent by email
CREATE INDEX idx_agents_email_lower
    ON qa_scoring.agents (LOWER(email));

-- Find evaluations by evaluator (manager)
CREATE INDEX idx_evaluations_evaluator
    ON qa_scoring.evaluations (LOWER(evaluator_email));

-- Find evaluations by source (manual vs ai)
CREATE INDEX idx_evaluations_source
    ON qa_scoring.evaluations (source);

-- Find evaluations by date range
CREATE INDEX idx_evaluations_created
    ON qa_scoring.evaluations (created_at DESC);

-- Scores per section (for category trend analysis)
CREATE INDEX idx_scores_section
    ON qa_scoring.evaluation_scores (section_id, evaluation_id);

-- Dialpad call ID lookup (for deduplication)
CREATE INDEX idx_evaluations_dialpad_call
    ON qa_scoring.evaluations (dialpad_call_id)
    WHERE dialpad_call_id IS NOT NULL;

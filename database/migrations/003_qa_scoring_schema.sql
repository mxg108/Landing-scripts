-- ============================================================================
-- Migration 003: qa_scoring schema
-- Database: landing_member_support_db (Railway Postgres)
--
-- This schema stores QA evaluations for call center agents. It serves
-- both the existing manual QA flow (Google Form + Apps Script) and the
-- AI-Scoring pipeline (Gemini 2.5 Flash).
--
-- Tables:
--   agents              Agent directory (source of truth for name/email)
--   evaluations         One row per QA evaluation (manual or AI)
--   evaluation_scores   One row per scored section per evaluation
--   audit_log           Request-level audit trail with cost attribution
--
-- Design principles:
--   - Multi-team from day one: team_id on agents and evaluations
--   - Store call metadata from day one (enables stratified sampling later)
--   - Documentation (section 9) is always manual — stored but never AI-scored
--   - Overall score is stored (not recalculated) for historical accuracy
--   - Agent history queries must be fast (indexed by agent + timestamp)
--   - Caller metadata persisted at scoring time for DataPoint drill-down
--   - Per-evaluation cost tracking for budget caps and exec reporting
--
-- Updated: 2026-04-06 — added multi-team support, caller metadata,
--   call_summary, cost tracking, audit_log table per PRD-MultiTeam.md
-- ============================================================================

CREATE SCHEMA IF NOT EXISTS qa_scoring;

-- ── agents ───────────────────────────────────────────────────────────────────
-- Source of truth for agent identity. Maps to the "Mails" sheet today.
-- Agents can change teams/managers over time, so evaluator is stored
-- per-evaluation, not per-agent. Supervisor is stored here because it
-- changes infrequently and is used for team dashboard filtering.

CREATE TABLE qa_scoring.agents (
    id              SERIAL PRIMARY KEY,
    team_id         TEXT NOT NULL DEFAULT 'member_support',
    name            TEXT NOT NULL,
    email           TEXT NOT NULL,
    supervisor      TEXT,
    active          BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Agent name is unique within a team, not globally
    UNIQUE (team_id, name)
);

-- ── evaluations ──────────────────────────────────────────────────────────────
-- One row per QA evaluation. Captures everything about a single scored call.
-- Supports both manual (Google Form) and AI-generated evaluations.
--
-- The overall_score is stored as-is (not recalculated on read) so that
-- historical scores remain stable even if the formula changes.

CREATE TABLE qa_scoring.evaluations (
    id                  SERIAL PRIMARY KEY,

    -- Team scoping (redundant with agent.team_id but avoids a JOIN
    -- on every query — evaluations are the hot table)
    team_id             TEXT NOT NULL DEFAULT 'member_support',

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

    -- Caller metadata (from Dialpad API get_call_details at scoring time)
    caller_name         TEXT,
    caller_phone        TEXT,

    -- Gemini-generated call summary (2-4 sentences: context, outcome, key moments)
    call_summary        TEXT,

    -- SOP used for scoring (NULL if none injected, populated by RAG pipeline)
    sop_used            TEXT,

    -- Sampling and workflow status (Phase 4)
    sampling_status     TEXT DEFAULT 'not_sampled',
    scoring_status      TEXT DEFAULT 'complete',
    flagged_long_call   BOOLEAN DEFAULT FALSE,

    -- Overall score (0-100 scale, calculated from numeric sections)
    overall_score       NUMERIC(5,1) NOT NULL,

    -- Qualitative feedback
    key_strengths       TEXT,
    improvements        TEXT,

    -- Cost tracking (estimated USD for the Gemini API call that scored this eval)
    estimated_cost_usd  NUMERIC(8,4),

    -- Audit
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── evaluation_scores ────────────────────────────────────────────────────────
-- One row per scored section per evaluation. Normalized so we can:
--   - Query trends per category across time
--   - Add new sections without schema changes (team-specific rubrics)
--   - Store AI confidence and reasoning per section
--
-- section_id values (Member Support):
--   Numeric (1-5): greeting, call_purpose, match_moment, process_adherence,
--                   call_resolution, communication, efficiency, documentation
--   Binary (Y/N):  identity_validation, customer_resolution
--
-- Other teams will have different section_id values defined in their
-- team config JSON (e.g. Sales: needs_discovery, objection_handling, etc.)

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

-- ── audit_log ────────────────────────────────────────────────────────────────
-- Request-level audit trail. Replaces the JSONL file (Step 1) with a
-- persistent, queryable table. Supports cost attribution per team and
-- executive cost reporting (Step 7).
--
-- Every API request that hits an authenticated endpoint is logged here.
-- Scoring requests additionally capture model, cost, and call context.

CREATE TABLE qa_scoring.audit_log (
    id              SERIAL PRIMARY KEY,
    timestamp       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    team_id         TEXT,
    endpoint        TEXT NOT NULL,
    method          TEXT NOT NULL,
    status_code     SMALLINT,
    duration_ms     NUMERIC(10,1),

    -- Auth context
    api_key_team    TEXT,

    -- Scoring-specific fields (NULL for non-scoring requests)
    action          TEXT,
    model           TEXT,
    estimated_cost_usd NUMERIC(8,4),
    call_id         TEXT,
    agent_name      TEXT,

    -- Error context (NULL on success)
    error_detail    TEXT
);


-- ── Indexes ──────────────────────────────────────────────────────────────────

-- Agent lookup by team (most queries are team-scoped)
CREATE INDEX idx_agents_team
    ON qa_scoring.agents (team_id);

-- Agent history: last N evaluations for an agent, newest first
-- This is the primary query pattern (ProgressionCard, "history per agent")
CREATE INDEX idx_evaluations_agent_date
    ON qa_scoring.evaluations (agent_id, created_at DESC);

-- Team-scoped evaluation queries (dashboard, stats)
CREATE INDEX idx_evaluations_team_date
    ON qa_scoring.evaluations (team_id, created_at DESC);

-- Lookup agent by name (case-insensitive, within a team)
CREATE INDEX idx_agents_team_name_lower
    ON qa_scoring.agents (team_id, LOWER(name));

-- Lookup agent by email
CREATE INDEX idx_agents_email_lower
    ON qa_scoring.agents (LOWER(email));

-- Find evaluations by evaluator (manager)
CREATE INDEX idx_evaluations_evaluator
    ON qa_scoring.evaluations (LOWER(evaluator_email));

-- Find evaluations by source (manual vs ai)
CREATE INDEX idx_evaluations_source
    ON qa_scoring.evaluations (source);

-- Find evaluations by date range (team-scoped)
CREATE INDEX idx_evaluations_created
    ON qa_scoring.evaluations (created_at DESC);

-- Scores per section (for category trend analysis)
CREATE INDEX idx_scores_section
    ON qa_scoring.evaluation_scores (section_id, evaluation_id);

-- Dialpad call ID lookup (for deduplication and DataPoint drill-down)
CREATE INDEX idx_evaluations_dialpad_call
    ON qa_scoring.evaluations (dialpad_call_id)
    WHERE dialpad_call_id IS NOT NULL;

-- Audit log: per-team cost queries (monthly rollups)
CREATE INDEX idx_audit_team_timestamp
    ON qa_scoring.audit_log (team_id, timestamp DESC);

-- Audit log: find all requests for a specific call
CREATE INDEX idx_audit_call
    ON qa_scoring.audit_log (call_id)
    WHERE call_id IS NOT NULL;

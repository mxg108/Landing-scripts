-- ============================================================================
-- Migration 006: qa.* tables
-- Spec: database/SQLMigration.md §3, §3.4.3, §3.12, §3.13, §7.6, §9.2
--
-- Nine tables that replace the Sheets-as-DB QA pipeline:
--
--   agents                       Replaces per-team Mails sheet (§3.3)
--   formula_versions             Archived overall-score formula JSON per
--                                team, write-path from FastAPI startup
--                                loader (§3.12)
--   evaluations                  One row per scored call across its full
--                                lifecycle (draft→approved→finalized),
--                                replacing the FR-AI + Score destination +
--                                Analyst_History row triplet (§3.4)
--   evaluation_sections          Normalized per-section scores with per-
--                                section provider provenance (§3.5)
--   formula_compliance_sweeps    Historic-compliance sweep results,
--                                one row per (evaluation × formula_version)
--                                — replaces v1's transient parity column,
--                                preserves iteration history (§3.13)
--   score_audit                  Action-level audit log, 1:1 with the
--                                Sheets `Score_Audit` tab (§3.9)
--   score_audit_archive          Same shape as score_audit; rows >180d
--                                are moved here by a daily cron (§3.9)
--   api_audit_log                Request-level audit + cost attribution;
--                                permanent retention (§3.10)
--   agent_stat_points            Per-finalize EWMA/SPC point per agent;
--                                drives CC sparklines, outlier chiclets,
--                                PDF assessments (§9.2)
--
-- CHECK constraints are the relaxed-pre-cutover variants (§3.4.3). The
-- strict post-cutover tightening lands in migration 012 per team via
-- ADD CONSTRAINT … NOT VALID + VALIDATE CONSTRAINT.
--
-- §3.4.1 partial UNIQUE dedupe indexes ship with this migration (not
-- deferred to 008) because they enforce schema invariants, not perf.
-- Analytics indexes (§9.1 + §9.2 idx_stat_points_agent + §3.12 +
-- §3.13 lookup indexes) ship in migration 008.
-- ============================================================================


-- ── qa.agents (§3.3) ────────────────────────────────────────────────────────
-- Replaces the per-team Mails sheet. UNIQUE on (team_id, LOWER(name)) so
-- agent lookups by name are case-insensitive without changing call sites.

CREATE TABLE qa.agents (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    team_id             TEXT NOT NULL REFERENCES public.teams(id),
    name                TEXT NOT NULL,
    canonical_name      TEXT,
    email               TEXT NOT NULL,
    supervisor_email    TEXT,
    -- Eager-resolved at Stage 1 per §3.11; nightly sweep as backstop.
    dialpad_agent_id    TEXT,
    active              BOOLEAN NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX uq_agents_team_lower_name
    ON qa.agents (team_id, LOWER(name));


-- ── qa.formula_versions (§3.12) ─────────────────────────────────────────────
-- Archived formula JSONs. Write path: FastAPI startup loader hashes the
-- loaded JSON; if no row matches (team_id, formula_version, hash), insert
-- it and mark any prior version's `effective_until = NOW()`. Old rows
-- remain reproducible forever — even after their section IDs disappear
-- from the active team config.
--
-- `formula_version` is UNIQUE NOT NULL so qa.evaluations.formula_version
-- can FK to it directly (FK by string).

CREATE TABLE qa.formula_versions (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    formula_version     TEXT NOT NULL UNIQUE,
    team_id             TEXT NOT NULL REFERENCES public.teams(id),
    formula_json        JSONB NOT NULL,
    effective_from      TIMESTAMPTZ NOT NULL,
    effective_until     TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- ── qa.evaluations (§3.4) ───────────────────────────────────────────────────
-- One row per call across its entire lifecycle. CHECKs are the §3.4.3
-- relaxed pre-cutover form — every evaluation passes through an
-- approved-with-NULL-overall_score window because Stage 3 (ARRAYFORMULA
-- readback) is a separate call. Migration 012 tightens these per team
-- once Phase C completes.

CREATE TABLE qa.evaluations (
    id                            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    team_id                       TEXT NOT NULL REFERENCES public.teams(id),
    -- agent_id may stay NULL until Stage 4 finalize resolves it via Mails
    -- lookup; do not enforce NOT NULL here.
    agent_id                      BIGINT REFERENCES qa.agents(id),
    agent_name_raw                TEXT NOT NULL,
    agent_email                   TEXT,
    evaluator_email               TEXT,
    state                         TEXT NOT NULL,
    source                        TEXT NOT NULL,
    -- Call lifecycle clocks (§3.4 + CallTimeOnAnalystHistory.md).
    call_connected_at             TIMESTAMPTZ,
    call_started_at               TIMESTAMPTZ,
    call_ended_at                 TIMESTAMPTZ,
    call_duration_ms              INTEGER,
    call_type                     TEXT,
    -- en / es / future; writer sets at Stage 1, no default — silent
    -- defaults would mask language-detection bugs.
    language                      TEXT,
    -- Dialpad ids — durable join keys, even when CC didn't see the call live.
    dialpad_call_id               TEXT,
    dialpad_master_call_id        TEXT,
    dialpad_entry_point_call_id   TEXT,
    -- Transition-period dedupe key (§3.4.1). NULL allowed for backfilled
    -- rows whose link couldn't be reconstructed.
    dialpad_link                  TEXT,
    -- Cross-schema nullable FK to command_center.calls (§3.4 + §4.2).
    -- NULL on Phase B backfilled rows that pre-date CC and on rows
    -- written during a CC outage. Stage 1 writer flips
    -- command_center.calls.scored = TRUE in the same transaction.
    command_center_call_id        BIGINT REFERENCES command_center.calls(id),
    mos_score                     NUMERIC(3,2),
    -- Normalized {audio: [...], screen: [...]} per §3.4.2; full original
    -- (recording_details with ids, durations, start_time) lives in
    -- dialpad_call_metadata for forensics.
    recording_urls                JSONB,
    -- Forward-compat catch-all (§3.7).
    dialpad_call_metadata         JSONB,
    caller_name                   TEXT,
    caller_phone                  TEXT,
    caller_email                  TEXT,
    call_summary                  TEXT,
    -- LandGPT Qwen2-Audio output. NULL pre-LandGPT and on Gemini-only
    -- rows. Schema in §8.2.
    annotated_transcript          JSONB,
    key_strengths                 TEXT,
    -- Renamed from §003 stub's `improvements`.
    opportunities                 TEXT,
    overall_score                 NUMERIC(5,1),
    -- FK-by-string to qa.formula_versions(formula_version). NULL on
    -- pre-cutover and backfilled historic rows whose implicit Sheet
    -- formula has no version row.
    formula_version               TEXT REFERENCES qa.formula_versions(formula_version),
    -- Cascade provenance per §8.1. NOT NULL because every scored
    -- evaluation has at least the text model recorded — even pre-LandGPT,
    -- this is `{"text": {"provider":"gemini","model":"gemini-2.5-flash"}}`.
    models_used                   JSONB NOT NULL,
    -- Indexable convenience summary of models_used. NULL allowed because
    -- legacy rows backfilled from Sheets won't have cascade provenance.
    ai_provider_primary           TEXT,
    -- Cloud-API cost (Gemini); NULL for pure-LandGPT runs (§8.1).
    estimated_cost_usd            NUMERIC(8,4),
    csat_score                    NUMERIC(3,1),
    -- Future FK to embeddings.sop_documents(id) — NOT declared as a FK
    -- here because 006/007 are independent (§7.6). Column ships now so
    -- the LandGPT v2 RAG cutover doesn't need an evaluations ALTER.
    sop_used_document_id          BIGINT,
    sampling_status               TEXT NOT NULL DEFAULT 'not_sampled',
    scoring_status                TEXT NOT NULL DEFAULT 'complete',
    created_at                    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    approved_at                   TIMESTAMPTZ,
    finalized_at                  TIMESTAMPTZ,
    -- §3.4.3 relaxed pre-cutover state CHECKs ----------------------------
    CONSTRAINT evaluations_state_check
        CHECK (state IN ('draft', 'approved', 'finalized')),
    CONSTRAINT evaluations_source_check
        CHECK (source IN ('ai', 'manual', 'ai_reviewed')),
    CONSTRAINT evaluations_finalized_requires_score
        CHECK (state <> 'finalized' OR overall_score IS NOT NULL),
    CONSTRAINT evaluations_approved_requires_evaluator_and_approved_at
        CHECK (state = 'draft'
               OR (evaluator_email IS NOT NULL AND approved_at IS NOT NULL)),
    CONSTRAINT evaluations_finalized_requires_finalized_at
        CHECK (state <> 'finalized' OR finalized_at IS NOT NULL),
    CONSTRAINT evaluations_scoring_status_check
        CHECK (scoring_status IN ('complete', 'flagged_long_call', 'errored',
                                  'landgpt_unavailable_routed_to_gemini')),
    CONSTRAINT evaluations_ai_provider_primary_check
        CHECK (ai_provider_primary IS NULL OR ai_provider_primary IN
               ('gemini', 'landgpt', 'landgpt_with_gemini_fallback'))
);

-- §3.4.1 dedupe — both partial UNIQUEs ship together; Phase B
-- reconciliation decides which to drop later.
CREATE UNIQUE INDEX uq_eval_team_call_id
    ON qa.evaluations (team_id, dialpad_call_id)
    WHERE dialpad_call_id IS NOT NULL;

CREATE UNIQUE INDEX uq_eval_team_link
    ON qa.evaluations (team_id, dialpad_link)
    WHERE dialpad_link IS NOT NULL;


-- ── qa.evaluation_sections (§3.5) ───────────────────────────────────────────
-- Normalized per-section rows. `ai_provider` is per-section because Plan B
-- (§8.3a) can route specific sections (e.g. matching_the_moment) back to
-- Gemini while others stay on LandGPT.

CREATE TABLE qa.evaluation_sections (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    evaluation_id   BIGINT NOT NULL REFERENCES qa.evaluations(id) ON DELETE CASCADE,
    section_id      TEXT NOT NULL,
    section_number  SMALLINT NOT NULL,
    score_type      TEXT NOT NULL,
    numeric_score   SMALLINT,
    binary_value    TEXT,
    score_source    TEXT NOT NULL,
    -- Populated when score_source='ai' (per CHECK below); NULL otherwise.
    ai_provider     TEXT,
    -- Specific model identifier within the provider, e.g. gemini-2.5-flash
    -- or gemma-4-27b-q4. NULL for non-AI sections.
    model           TEXT,
    confidence      TEXT,
    reasoning       TEXT,
    CONSTRAINT eval_sections_score_type_check
        CHECK (score_type IN ('numeric', 'binary',
                              'manual_numeric', 'manual_binary',
                              'auto_value')),
    CONSTRAINT eval_sections_score_source_check
        CHECK (score_source IN ('ai', 'manual', 'auto_value', 'manual_default')),
    CONSTRAINT eval_sections_numeric_range_check
        CHECK (numeric_score IS NULL OR numeric_score BETWEEN 1 AND 5),
    CONSTRAINT eval_sections_binary_value_check
        CHECK (binary_value IS NULL OR binary_value IN ('Y', 'N', 'NA')),
    -- "Exactly one of numeric_score/binary_value populated, matching score_type"
    --   numeric / manual_numeric → numeric_score required, binary NULL
    --   binary  / manual_binary  → binary_value required, numeric NULL
    --   auto_value               → exactly one of the two populated
    --                              (the section's underlying shape decides)
    CONSTRAINT eval_sections_value_matches_type_check CHECK (
        (score_type IN ('numeric', 'manual_numeric')
            AND numeric_score IS NOT NULL AND binary_value IS NULL)
        OR (score_type IN ('binary', 'manual_binary')
            AND binary_value IS NOT NULL AND numeric_score IS NULL)
        OR (score_type = 'auto_value'
            AND ((numeric_score IS NOT NULL AND binary_value IS NULL)
              OR (binary_value IS NOT NULL AND numeric_score IS NULL)))
    ),
    -- `ai_provider` iff `score_source='ai'`.
    CONSTRAINT eval_sections_ai_provider_iff_ai CHECK (
        (score_source = 'ai' AND ai_provider IS NOT NULL)
        OR (score_source <> 'ai' AND ai_provider IS NULL)
    ),
    CONSTRAINT eval_sections_ai_provider_value_check
        CHECK (ai_provider IS NULL OR ai_provider IN ('gemini', 'landgpt')),
    CONSTRAINT uq_eval_sections_eval_section UNIQUE (evaluation_id, section_id)
);


-- ── qa.formula_compliance_sweeps (§3.13, new in v1.1) ───────────────────────
-- Persistent record of every historic-compliance sweep run during Phase
-- A.5. One row per (evaluation × swept formula version). Replaces v1's
-- transient overall_score_parity_check_value column so iteration history
-- is preserved across formula revisions.

CREATE TABLE qa.formula_compliance_sweeps (
    id                      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    evaluation_id           BIGINT NOT NULL REFERENCES qa.evaluations(id) ON DELETE CASCADE,
    swept_formula_version   TEXT NOT NULL REFERENCES qa.formula_versions(formula_version),
    recomputed_score        NUMERIC(5,1) NOT NULL,
    original_score          NUMERIC(5,1) NOT NULL,
    -- Signed delta — sign matters for pattern-spotting (new > sheet vs
    -- new < sheet are different stories).
    delta                   NUMERIC(6,2) GENERATED ALWAYS AS
                            (recomputed_score - original_score) STORED,
    -- ε pinned per sweep for reproducibility (default 0.05 per spec).
    epsilon                 NUMERIC(4,2) NOT NULL DEFAULT 0.05,
    -- ABS(delta) > epsilon, persisted at sweep time so iteration-rate
    -- queries don't have to recompute.
    flagged                 BOOLEAN NOT NULL,
    swept_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- Re-running the sweep for the same (eval, version) is a no-op via
    -- ON CONFLICT DO NOTHING on this UNIQUE.
    CONSTRAINT uq_sweeps_eval_version UNIQUE (evaluation_id, swept_formula_version)
);


-- ── qa.score_audit (§3.9) ───────────────────────────────────────────────────
-- Action-level log. 1:1 with today's Sheets `Score_Audit` tab; the column
-- list mirrors `backend/config/score_audit.py:COLUMNS`. Retention: 180
-- days hot (daily cron MOVEs older rows to score_audit_archive).
--
-- v0.6 added `evaluation_orphaned` action when a qa.evaluations row is
-- deleted (admin-only path).

CREATE TABLE qa.score_audit (
    id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    timestamp         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    api_key_role      TEXT NOT NULL,
    evaluator_email   TEXT,
    agent_email       TEXT,
    agent_name        TEXT,
    call_id           TEXT,
    target_team       TEXT,
    action            TEXT NOT NULL,
    result_row        INTEGER,
    notes             TEXT,
    CONSTRAINT score_audit_action_check
        CHECK (action IN ('scored', 'denied', 'approved', 'evaluation_orphaned'))
);


-- ── qa.score_audit_archive (§3.9) ───────────────────────────────────────────
-- Same shape as score_audit; rows moved here by daily cron after 180d.
-- Search endpoint UNIONs both tables.

CREATE TABLE qa.score_audit_archive (
    id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- Preserves the original score_audit.id for forensic continuity, but
    -- not enforced as UNIQUE — the archive is append-only with rare
    -- compaction.
    original_id       BIGINT NOT NULL,
    timestamp         TIMESTAMPTZ NOT NULL,
    api_key_role      TEXT NOT NULL,
    evaluator_email   TEXT,
    agent_email       TEXT,
    agent_name        TEXT,
    call_id           TEXT,
    target_team       TEXT,
    action            TEXT NOT NULL,
    result_row        INTEGER,
    notes             TEXT,
    archived_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT score_audit_archive_action_check
        CHECK (action IN ('scored', 'denied', 'approved', 'evaluation_orphaned'))
);


-- ── qa.api_audit_log (§3.10) ────────────────────────────────────────────────
-- Request-level audit + cost attribution. PERMANENT retention (changed
-- in v0.5) — multi-year visibility for "what would model X have cost us
-- last year?" comparisons. CC startup-replay tripwire lands here too
-- with endpoint='cc.startup_replay', action='replay'.

CREATE TABLE qa.api_audit_log (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    timestamp           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    team_id             TEXT,
    endpoint            TEXT NOT NULL,
    method              TEXT NOT NULL,
    status_code         SMALLINT,
    duration_ms         NUMERIC(10,1),
    api_key_team        TEXT,
    api_key_role        TEXT,
    action              TEXT,
    model               TEXT,
    estimated_cost_usd  NUMERIC(8,4),
    call_id             TEXT,
    agent_name          TEXT,
    error_detail        TEXT
);


-- ── qa.agent_stat_points (§9.2) ─────────────────────────────────────────────
-- One row per evaluation finalize. team_stats.py keeps owning the math;
-- what changes is it computes ONE point on each finalize. Consumers:
-- CC Zone D sparklines, QA outlier chiclets, PDF assessment pipeline.
--
-- `coverage_regime` is hardcoded to 'manager_sample' today; per the
-- [[project_predictive_groundwork]] memory it moves per-row at the Local
-- AI cutover (~July 2026) so old manager-sampled rows and new full-
-- coverage rows stay distinguishable downstream.

CREATE TABLE qa.agent_stat_points (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    team_id         TEXT NOT NULL REFERENCES public.teams(id),
    agent_id        BIGINT NOT NULL REFERENCES qa.agents(id),
    -- UNIQUE because each evaluation finalize produces at most one point.
    evaluation_id   BIGINT NOT NULL UNIQUE REFERENCES qa.evaluations(id),
    score           NUMERIC(5,1) NOT NULL,
    ewma            NUMERIC(6,2) NOT NULL,
    -- Pinned per point so historical series are reproducible even when
    -- team_stats.py changes the default λ.
    ewma_lambda     NUMERIC(4,3) NOT NULL,
    spc_mean        NUMERIC(6,2),
    spc_sigma       NUMERIC(6,3),
    spc_flags       TEXT[],
    coverage_regime TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

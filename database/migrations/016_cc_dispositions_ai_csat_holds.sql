-- ============================================================================
-- Migration 016: CC v1 dispositions, AI-CSAT, hold_intervals
-- Spec: qa-automation/AI-Scoring/references/DispositionDesign.md §3 (v2.1)
--
-- Three surfaces:
--
--   command_center.calls        +disposition_category/+disposition (split
--                               from Dialpad's `Category~Sub` form),
--                               +ai_csat, +disposition_source provenance.
--   qa.evaluations              +dialpad_disposition_category /
--                               +dialpad_disposition / +ai_csat — stamped at
--                               Stage 1 from the CC match so the eval row is
--                               self-contained for analytics/one-pagers.
--   command_center.hold_intervals   RETURNS. Scrapped in SQLMigration §4.7
--                               with an explicit bring-it-back clause; the
--                               consumer is now scoring-prompt grounding,
--                               which needs per-cycle timing (WHERE in the
--                               call holds happened), not just the
--                               total_hold_seconds rollup.
-- ============================================================================


-- ── command_center.calls — disposition + AI-CSAT columns ────────────────────
-- No enum CHECK on the label values: dispositions are Dialpad-admin-editable
-- (9 categories × ~50 subdispositions today) and will drift; a CHECK would
-- turn every ops edit into a migration. disposition_source IS ours, so it
-- does get one.
--
-- Bare-category selections (agent stopped at level 1) leave `disposition`
-- NULL with `disposition_category` set. All-NULL is expected-normal, not an
-- error: an agent pulled into a new call from the disposition-select screen
-- leaves the previous call undispositioned (~17% of calls in the 2026-07-15
-- sample).

ALTER TABLE command_center.calls
    ADD COLUMN disposition_category TEXT,
    ADD COLUMN disposition          TEXT,
    -- Dialpad Ai CSAT estimate. Distinct from the user-survey CSAT (which
    -- has a survey_id and stays out of scope) — do NOT conflate with
    -- qa.evaluations.csat_score.
    ADD COLUMN ai_csat              NUMERIC(3,1),
    -- Provenance for the backfill-vs-live seam:
    --   webhook      stamped live by the call-event fold (C2)
    --   stats_pull   filled by scripts/pull_dispositions.py (C4)
    ADD COLUMN disposition_source   TEXT,
    ADD CONSTRAINT calls_disposition_source_check
        CHECK (disposition_source IN ('webhook', 'stats_pull')),
    -- A disposition never arrives without provenance, and provenance is
    -- meaningless without a disposition.
    ADD CONSTRAINT calls_disposition_provenance_pair_check
        CHECK ((disposition_category IS NULL) = (disposition_source IS NULL)),
    -- A subdisposition is level 2 of the `Category~Sub` form — it cannot
    -- exist without its category.
    ADD CONSTRAINT calls_subdisposition_requires_category_check
        CHECK (disposition IS NULL OR disposition_category IS NOT NULL);

-- §5 triple-key match (entry_point → call_id → master): dialpad_call_id is
-- covered by uq_calls_team_call_id (005); these add the other two probes,
-- extending 014's qa.evaluations entry-point index pattern to CC.
CREATE INDEX idx_calls_entry_point_call_id
    ON command_center.calls (team_id, dialpad_entry_point_call_id)
    WHERE dialpad_entry_point_call_id IS NOT NULL;

CREATE INDEX idx_calls_master_call_id
    ON command_center.calls (team_id, dialpad_master_call_id)
    WHERE dialpad_master_call_id IS NOT NULL;


-- ── qa.evaluations — Stage-1 stamps from the CC match ───────────────────────
-- Same reproducibility instinct as the formula/rubric stamps: the eval row
-- is self-contained even if the CC row is later updated.

ALTER TABLE qa.evaluations
    ADD COLUMN dialpad_disposition_category TEXT,
    ADD COLUMN dialpad_disposition          TEXT,
    -- Dialpad Ai estimate — csat_score (006) remains the user-survey slot.
    ADD COLUMN ai_csat                      NUMERIC(3,1);


-- ── command_center.hold_intervals — per-cycle hold detail ───────────────────
-- Derivation rule (§4.1, tested in C2): no `unhold` event exists — a hold
-- cycle ends at the next `connected` (agent reconnected) or `hangup`
-- (call ended on hold). Rows materialize at ingest time when the ending
-- event arrives, so every row is a COMPLETED cycle: started_at/ended_at/
-- seconds/ended_by are all NOT NULL. In-flight holds live in the calls
-- row's last_state until they close. total_hold_seconds on `calls` stays
-- as the rollup.

CREATE TABLE command_center.hold_intervals (
    id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    team_id          TEXT NOT NULL REFERENCES public.teams(id),
    dialpad_call_id  TEXT NOT NULL,
    call_id          BIGINT NOT NULL REFERENCES command_center.calls(id)
                     ON DELETE CASCADE,
    started_at       TIMESTAMPTZ NOT NULL,
    ended_at         TIMESTAMPTZ NOT NULL,
    seconds          INTEGER NOT NULL,
    ended_by         TEXT NOT NULL,
    CONSTRAINT hold_intervals_ended_by_check
        CHECK (ended_by IN ('connected', 'hangup')),
    CONSTRAINT hold_intervals_ordered_check
        CHECK (ended_at >= started_at),
    CONSTRAINT hold_intervals_seconds_non_negative_check
        CHECK (seconds >= 0)
);

-- The §5 grounding pull fetches a matched call's cycles by FK.
CREATE INDEX idx_hold_intervals_call_id
    ON command_center.hold_intervals (call_id);

-- Direct probe for consumers that only hold the Dialpad id (C4 backfill
-- spot-checks, ad-hoc ops queries).
CREATE INDEX idx_hold_intervals_team_call
    ON command_center.hold_intervals (team_id, dialpad_call_id);

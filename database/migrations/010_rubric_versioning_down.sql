-- ============================================================================
-- Migration 010 — DOWN
--
-- Reverses 010 in dependency order:
--   1) Drop qa.evaluations.rubric_version FK (must come before dropping
--      qa.rubric_versions — the FK depends on the target table).
--   2) Drop qa.rubric_versions (cascades the seed rows + the
--      idx_rubric_versions_team_effective index).
--   3) Drop the public.teams operational columns added in 010.
--
-- WARNING: if any qa.evaluations row has a non-NULL rubric_version when
-- down runs, dropping the column erases that data — but the rows
-- themselves are preserved (only the version stamp is lost). v1.3
-- shipped before Wave-2 dual-write started writing evals, so in normal
-- prod state qa.evaluations is empty and this is a no-op.
-- ============================================================================

ALTER TABLE qa.evaluations DROP COLUMN IF EXISTS rubric_version;

DROP TABLE IF EXISTS qa.rubric_versions;

ALTER TABLE public.teams
    DROP COLUMN IF EXISTS updated_at,
    DROP COLUMN IF EXISTS sheets_config,
    DROP COLUMN IF EXISTS excluded_test_agents,
    DROP COLUMN IF EXISTS gemini_config,
    DROP COLUMN IF EXISTS stats_config,
    DROP COLUMN IF EXISTS company;

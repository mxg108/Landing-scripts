-- 012: allow NA on numeric sections in qa.evaluation_sections
--
-- §3.8 point 7 (SQLMigration.md): sections with na_default (currently only
-- human_review_required) are created at Stage 1 with numeric_score = NULL /
-- binary_value = 'NA' — NA is the *default* state of every MS evaluation's
-- human_review_required row. The 006 value-matches-type CHECK predates that
-- decision and rejects the combination for numeric score_types.
--
-- This widens the CHECK with one branch: numeric / manual_numeric rows may
-- carry binary_value = 'NA' (and only 'NA') with numeric_score NULL.
-- Y/N on numeric rows remains invalid, as does NA alongside a numeric score.
--
-- (The "migration 012" mentioned in 006's comments — post-Phase-C per-team
-- state CHECK tightening — is unrelated forward-reference prose; that work
-- will take its own number when Phase C lands.)

ALTER TABLE qa.evaluation_sections
    DROP CONSTRAINT eval_sections_value_matches_type_check;

ALTER TABLE qa.evaluation_sections
    ADD CONSTRAINT eval_sections_value_matches_type_check CHECK (
        (score_type IN ('numeric', 'manual_numeric')
            AND numeric_score IS NOT NULL AND binary_value IS NULL)
        OR (score_type IN ('numeric', 'manual_numeric')
            AND numeric_score IS NULL AND binary_value = 'NA')
        OR (score_type IN ('binary', 'manual_binary')
            AND binary_value IS NOT NULL AND numeric_score IS NULL)
        OR (score_type = 'auto_value'
            AND ((numeric_score IS NOT NULL AND binary_value IS NULL)
              OR (binary_value IS NOT NULL AND numeric_score IS NULL)))
    );

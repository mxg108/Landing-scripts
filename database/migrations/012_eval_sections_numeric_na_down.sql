-- 012 down: restore the 006 value-matches-type CHECK.
--
-- Fails if any numeric-type row with binary_value = 'NA' exists — those rows
-- must be deleted or rescored before rolling back.

ALTER TABLE qa.evaluation_sections
    DROP CONSTRAINT eval_sections_value_matches_type_check;

ALTER TABLE qa.evaluation_sections
    ADD CONSTRAINT eval_sections_value_matches_type_check CHECK (
        (score_type IN ('numeric', 'manual_numeric')
            AND numeric_score IS NOT NULL AND binary_value IS NULL)
        OR (score_type IN ('binary', 'manual_binary')
            AND binary_value IS NOT NULL AND numeric_score IS NULL)
        OR (score_type = 'auto_value'
            AND ((numeric_score IS NOT NULL AND binary_value IS NULL)
              OR (binary_value IS NOT NULL AND numeric_score IS NULL)))
    );

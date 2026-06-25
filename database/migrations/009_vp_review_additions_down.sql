-- ============================================================================
-- Migration 009 — DOWN
--
-- Drops every artifact 009's UP created, in reverse dependency order:
--   1) Drop new tables (coaching_evaluations first — FK to coachings).
--   2) Drop new indexes on qa.evaluations (the partial human-review queue).
--   3) Reverse the ALTER TABLE qa.evaluations changes:
--      - Drop the new columns + their CHECKs (CHECKs drop with the columns).
--      - Restore the original scoring_status CHECK (without 'flagged_human_review').
--
-- WARNING: if any qa.evaluations row has scoring_status='flagged_human_review',
-- the down will fail when re-adding the original CHECK constraint — rows
-- violating the old enum block the constraint validation. That's by design;
-- the operator must manually decide what to do with those rows (mark them
-- 'complete', delete them, or stop the rollback) before re-running down.
-- ============================================================================

-- Tables — coaching_evaluations FKs to coachings, drop in order
DROP TABLE IF EXISTS qa.coaching_evaluations;
DROP TABLE IF EXISTS qa.coachings;
DROP TABLE IF EXISTS qa.evaluation_tags;
DROP TABLE IF EXISTS qa.tags;

-- Partial index on qa.evaluations — drops cleanly even if rows exist.
DROP INDEX IF EXISTS qa.idx_eval_human_review_queue;

-- Reverse the ALTER TABLE qa.evaluations changes.
-- Restore scoring_status CHECK to its pre-009 form. If any rows hold
-- 'flagged_human_review', this ADD CONSTRAINT will fail at validation time.
ALTER TABLE qa.evaluations
    DROP CONSTRAINT IF EXISTS evaluations_scoring_status_check;

ALTER TABLE qa.evaluations
    ADD CONSTRAINT evaluations_scoring_status_check
    CHECK (scoring_status IN ('complete', 'flagged_long_call', 'errored',
                              'landgpt_unavailable_routed_to_gemini'));

-- Drop the v1.2 CHECK constraints (DROP COLUMN drops them anyway, but
-- being explicit makes the intent clear and survives partial down).
ALTER TABLE qa.evaluations
    DROP CONSTRAINT IF EXISTS evaluations_human_review_pair_check,
    DROP CONSTRAINT IF EXISTS evaluations_needs_coaching_check;

-- Drop the v1.2 columns.
ALTER TABLE qa.evaluations
    DROP COLUMN IF EXISTS human_review_completed_at,
    DROP COLUMN IF EXISTS human_review_required_at,
    DROP COLUMN IF EXISTS action_plan,
    DROP COLUMN IF EXISTS needs_coaching;

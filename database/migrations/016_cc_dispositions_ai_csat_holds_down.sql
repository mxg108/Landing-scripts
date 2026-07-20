-- ============================================================================
-- Migration 016 — DOWN
--
-- Drops hold_intervals and the disposition/AI-CSAT columns on both tables.
-- ⚠ Destroys any webhook-folded (C2) or stats-pulled (C4) disposition data
-- and materialized hold cycles — webhook_events remains the append-only
-- truth, so a re-apply + replay/backfill can rebuild all of it.
-- ============================================================================

DROP TABLE IF EXISTS command_center.hold_intervals;

DROP INDEX IF EXISTS command_center.idx_calls_entry_point_call_id;
DROP INDEX IF EXISTS command_center.idx_calls_master_call_id;

ALTER TABLE command_center.calls
    DROP COLUMN IF EXISTS disposition_category,
    DROP COLUMN IF EXISTS disposition,
    DROP COLUMN IF EXISTS ai_csat,
    DROP COLUMN IF EXISTS disposition_source;

ALTER TABLE qa.evaluations
    DROP COLUMN IF EXISTS dialpad_disposition_category,
    DROP COLUMN IF EXISTS dialpad_disposition,
    DROP COLUMN IF EXISTS ai_csat;

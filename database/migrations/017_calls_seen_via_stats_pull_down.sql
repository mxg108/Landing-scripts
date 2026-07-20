-- ============================================================================
-- Migration 017 — DOWN
--
-- ⚠ Fails (CheckViolation) if any seen_via='stats_pull' rows exist —
-- delete or re-tag them first; the restored CHECK must hold.
-- ============================================================================

ALTER TABLE command_center.calls
    DROP CONSTRAINT calls_seen_via_check,
    ADD CONSTRAINT calls_seen_via_check
        CHECK (seen_via IN ('webhook', 'qa_on_demand', 'qa_backfill'));

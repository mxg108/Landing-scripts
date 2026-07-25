-- ============================================================================
-- Migration 018 — DOWN
--
-- ⚠ The CHECK restores fail (CheckViolation) if any 'rescored' /
-- 'overridden' rows exist in either audit table — delete or re-tag them
-- first; the restored CHECKs must hold. Dropping auto_rescored_at
-- discards the once-ever latch history.
-- ============================================================================

ALTER TABLE qa.score_audit
    DROP CONSTRAINT score_audit_action_check,
    ADD CONSTRAINT score_audit_action_check
        CHECK (action IN ('scored', 'denied', 'approved',
                          'evaluation_orphaned'));

ALTER TABLE qa.score_audit_archive
    DROP CONSTRAINT score_audit_archive_action_check,
    ADD CONSTRAINT score_audit_archive_action_check
        CHECK (action IN ('scored', 'denied', 'approved',
                          'evaluation_orphaned'));

ALTER TABLE qa.evaluations
    DROP COLUMN auto_rescored_at;

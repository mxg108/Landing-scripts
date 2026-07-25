-- ============================================================================
-- Migration 018: scorecard lifecycle actions
-- Spec: qa-automation/AI-Scoring/references/ScorecardActionsDesign.md §6
--
-- (a) Audit action vocabulary — the rescore and override doorways get
--     explicit actions (explicit beats notes-parsing for audit search).
--     Applied to both the hot table and the archive twin so the 180-day
--     mover never trips the archive CHECK.
--
-- (b) qa.evaluations.auto_rescored_at — the once-EVER latch for the
--     ≤threshold auto-rescore (§4.2). Timestamp, not boolean: "when did
--     the machine take its one retry" is audit signal. Stamped BEFORE
--     the re-run is scheduled so a crash mid-rescore can't loop.
--
-- No other schema change: override rides source='ai_reviewed', rescore
-- reuses the Stage-1 upsert, and the coaching receipt (§4.3) uses
-- qa.coachings / qa.coaching_evaluations as-is.
-- ============================================================================

ALTER TABLE qa.score_audit
    DROP CONSTRAINT score_audit_action_check,
    ADD CONSTRAINT score_audit_action_check
        CHECK (action IN ('scored', 'denied', 'approved',
                          'evaluation_orphaned', 'rescored', 'overridden'));

ALTER TABLE qa.score_audit_archive
    DROP CONSTRAINT score_audit_archive_action_check,
    ADD CONSTRAINT score_audit_archive_action_check
        CHECK (action IN ('scored', 'denied', 'approved',
                          'evaluation_orphaned', 'rescored', 'overridden'));

ALTER TABLE qa.evaluations
    ADD COLUMN auto_rescored_at TIMESTAMPTZ;

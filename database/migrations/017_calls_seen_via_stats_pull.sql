-- ============================================================================
-- Migration 017: allow seen_via = 'stats_pull' on command_center.calls
--
-- Interim CC v1 ingestion (webhook receiver blocked on the Dialpad
-- subscription cap + Railway root-directory change): the Stats-API
-- dispositions puller now CREATES calls rows for calls CC never saw
-- live, so the C3 grounding match works backfill-first. Those rows need
-- honest provenance — 'qa_backfill' means "stub from Analyst_History"
-- and must not be overloaded.
--
-- Rows with seen_via='stats_pull' carry NO hold truth (the dispositions
-- export has no hold data); the grounding block branches on this.
-- ============================================================================

ALTER TABLE command_center.calls
    DROP CONSTRAINT calls_seen_via_check,
    ADD CONSTRAINT calls_seen_via_check
        CHECK (seen_via IN ('webhook', 'qa_on_demand', 'qa_backfill',
                            'stats_pull'));

-- ============================================================================
-- Migration 014: datapoint-lookup index (ReadPathFlip §3 W6, slice F5)
--
-- The /datapoints/{call_id} endpoint resolves a single evaluation by the
-- call id parsed from its Dialpad link. On the Postgres read path that id
-- equals dialpad_entry_point_call_id for the common case, but that column
-- had no index — so PostgresProvider.get_by_eval_id would seq-scan the
-- finalized-eval set on every datapoint-page load. dialpad_call_id is
-- already covered by uq_eval_team_call_id (006); this adds the matching
-- probe for the entry-point id so the lookup is a single indexed row hit.
--
-- Not CONCURRENTLY: same rationale as 008 — the runner wraps each
-- migration in a transaction (CONCURRENTLY can't run there), and
-- qa.evaluations is small (~2k finalized rows post-backfill), so the
-- brief ACCESS EXCLUSIVE lock is a non-issue.
-- ============================================================================

CREATE INDEX idx_eval_entry_point_call_id
    ON qa.evaluations (team_id, dialpad_entry_point_call_id)
    WHERE dialpad_entry_point_call_id IS NOT NULL;

-- ============================================================================
-- Migration 015 — DOWN
--
-- Drops both read views. ⚠ team_source.fetch_history_frame reads
-- qa.v_history_long since F5 — only roll back together with (or after)
-- reverting the F5 code, or the /team trio 500s.
-- ============================================================================

DROP VIEW IF EXISTS qa.v_monthly_scores;
DROP VIEW IF EXISTS qa.v_history_long;

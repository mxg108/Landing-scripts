-- ============================================================================
-- Migration 008 — DOWN
--
-- Drops every index this migration created. Order doesn't matter for
-- DROP INDEX. Down is a no-op for query correctness but restores the
-- "no analytics indexes" state for rerunning the index-tuning analysis.
-- ============================================================================

DROP INDEX IF EXISTS embeddings.idx_chunk_embeddings_hnsw_1024;
DROP INDEX IF EXISTS embeddings.idx_chunk_embeddings_hnsw_1536;
DROP INDEX IF EXISTS command_center.idx_frequent_callers_team_phone;
DROP INDEX IF EXISTS command_center.idx_webhook_replay;
DROP INDEX IF EXISTS command_center.idx_calls_agent;
DROP INDEX IF EXISTS command_center.idx_calls_team_connected;
DROP INDEX IF EXISTS command_center.idx_calls_team_scored_connected;
DROP INDEX IF EXISTS qa.idx_sweeps_eval;
DROP INDEX IF EXISTS qa.idx_sweeps_formula_flagged;
DROP INDEX IF EXISTS qa.idx_formula_versions_team_effective;
DROP INDEX IF EXISTS qa.idx_stat_points_agent;
DROP INDEX IF EXISTS qa.idx_sections_trend;
DROP INDEX IF EXISTS qa.idx_eval_agent_time;
DROP INDEX IF EXISTS qa.idx_eval_team_time;

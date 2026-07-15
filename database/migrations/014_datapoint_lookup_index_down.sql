-- ============================================================================
-- Migration 014 — DOWN
--
-- Drops the datapoint-lookup index. No-op for correctness (the provider
-- falls back to the scan path); restores the pre-W6 index state.
-- ============================================================================

DROP INDEX IF EXISTS qa.idx_eval_entry_point_call_id;

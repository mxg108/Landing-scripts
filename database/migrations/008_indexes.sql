-- ============================================================================
-- Migration 008: analytics + KNN indexes
-- Spec: database/SQLMigration.md §9.1, §9.2, §3.12, §3.13, §4.2, §4.1, §5.6
--
-- Indexes split out from 005/006/007 so that future re-indexing can use
-- CREATE INDEX CONCURRENTLY (this initial-deploy migration creates them
-- without CONCURRENTLY because the tables are empty — locking is a
-- non-issue on empty tables and CONCURRENTLY can't run inside the
-- runner's per-migration transaction).
--
-- Categories:
--   §9.1 read-path indexes on qa.evaluations + qa.evaluation_sections
--   §9.2 idx_stat_points_agent on qa.agent_stat_points
--   §3.12 lookup index on qa.formula_versions
--   §3.13 sweep indexes on qa.formula_compliance_sweeps
--   §4.2 calls perf indexes (ratio, date-range, per-agent)
--   §4.1 webhook_events replay-ordering index
--   §4.5 frequent_callers_cache lookup index
--   §5.6 HNSW KNN indexes per non-NULL dim column
-- ============================================================================


-- ── §9.1 read-path indexes on qa.evaluations ────────────────────────────────
-- These three drive agent-history queries, team-stats, and category
-- trend analysis. Partial WHERE state='finalized' keeps the indexes
-- tight — draft/approved rows are write-heavy and don't get read on
-- the analytical hot paths.

CREATE INDEX idx_eval_team_time
    ON qa.evaluations (team_id, finalized_at)
    WHERE state = 'finalized';

CREATE INDEX idx_eval_agent_time
    ON qa.evaluations (agent_id, finalized_at)
    WHERE state = 'finalized' AND agent_id IS NOT NULL;

CREATE INDEX idx_sections_trend
    ON qa.evaluation_sections (section_id, evaluation_id);


-- ── §9.2 agent_stat_points lookup ───────────────────────────────────────────
-- The CC sparkline path: "latest N points for this agent, in id order"
-- (id is monotonic with finalize time within an agent).

CREATE INDEX idx_stat_points_agent
    ON qa.agent_stat_points (agent_id, id);


-- ── §3.12 formula_versions per-team lookup ──────────────────────────────────
-- "Which formula version was current at time T for team X" reads via
-- this index ordered DESC by effective_from.

CREATE INDEX idx_formula_versions_team_effective
    ON qa.formula_versions (team_id, effective_from DESC);


-- ── §3.13 formula_compliance_sweeps lookup indexes ──────────────────────────
-- The flag-rate-per-formula-version query (the iteration-progress metric)
-- joins on (swept_formula_version, flagged) and aggregates. The
-- eval-id lookup serves "show me every sweep for this evaluation."

CREATE INDEX idx_sweeps_formula_flagged
    ON qa.formula_compliance_sweeps (swept_formula_version, flagged);

CREATE INDEX idx_sweeps_eval
    ON qa.formula_compliance_sweeps (evaluation_id);


-- ── §4.2 command_center.calls perf indexes ──────────────────────────────────
-- The ratio query (calls-received-vs-calls-scored) is leadership's
-- single-most-asked metric — this index turns it into milliseconds.

CREATE INDEX idx_calls_team_scored_connected
    ON command_center.calls (team_id, scored, connected_at);

-- Date-range queries (per-team daily volume, calls in window, etc.).
CREATE INDEX idx_calls_team_connected
    ON command_center.calls (team_id, connected_at DESC)
    WHERE connected_at IS NOT NULL;

-- Agent-level call volume queries (Zone D drill-down).
CREATE INDEX idx_calls_agent
    ON command_center.calls (team_id, dialpad_agent_id)
    WHERE dialpad_agent_id IS NOT NULL;


-- ── §4.1 webhook_events replay-ordering index ───────────────────────────────
-- call_state.rebuild() reads `WHERE team_id = ? AND event_timestamp >=
-- $today` ORDER BY event_timestamp, id. Perf budget per §4.1.2: p95 < 2s
-- at 10k events.

CREATE INDEX idx_webhook_replay
    ON command_center.webhook_events (team_id, event_timestamp, id);


-- ── §4.5 frequent_callers_cache per-team lookup ─────────────────────────────

CREATE INDEX idx_frequent_callers_team_phone
    ON command_center.frequent_callers_cache (team_id, caller_phone_e164);


-- ── §5.6 KNN HNSW indexes on the embedding_* dim columns ────────────────────
-- HNSW with default operator class `vector_cosine_ops` — cosine
-- similarity matches the SOP-retrieval use case. Partial WHERE NOT NULL
-- keeps each index focused on rows that actually carry that dim class
-- (the exactly-one-dim CHECK guarantees a row populates exactly one of
-- the two columns).

CREATE INDEX idx_chunk_embeddings_hnsw_1536
    ON embeddings.sop_chunk_embeddings
    USING hnsw (embedding_1536 vector_cosine_ops)
    WHERE embedding_1536 IS NOT NULL;

CREATE INDEX idx_chunk_embeddings_hnsw_1024
    ON embeddings.sop_chunk_embeddings
    USING hnsw (embedding_1024 vector_cosine_ops)
    WHERE embedding_1024 IS NOT NULL;

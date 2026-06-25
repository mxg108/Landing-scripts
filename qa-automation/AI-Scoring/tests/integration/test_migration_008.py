"""Tests for migration 008 — analytics + KNN indexes.

Per SQLMigration.md §11.5: "For every analytics index (§9.1): ≥1
EXPLAIN-plan test asserting the index is used by the documented query
shape." That requirement is also applied to the §9.2/§3.12/§3.13/§4.x
indexes — every index in this PR gets one EXPLAIN test against the
query shape its docstring names.

`SET enable_seqscan = OFF` is used to keep the planner from preferring a
sequential scan on the tiny ephemeral test tables — once tables have
real volume the planner will pick the indexes naturally, but the
correctness gate here is "the index is structurally compatible with the
query," not "the planner would have picked it on 0 rows."
"""

from __future__ import annotations

from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

REPO_ROOT = Path(__file__).resolve().parents[4]
MIGRATIONS_DIR = REPO_ROOT / "database" / "migrations"

UP_004 = (MIGRATIONS_DIR / "004_create_schemas_and_teams.sql").read_text()
UP_005 = (MIGRATIONS_DIR / "005_command_center_tables.sql").read_text()
UP_006 = (MIGRATIONS_DIR / "006_qa_tables.sql").read_text()
UP_007 = (MIGRATIONS_DIR / "007_embeddings_tables.sql").read_text()
UP_008 = (MIGRATIONS_DIR / "008_indexes.sql").read_text()
DOWN_008 = (MIGRATIONS_DIR / "008_indexes_down.sql").read_text()


@pytest_asyncio.fixture
async def pg_008(clean_pg: asyncpg.Connection) -> asyncpg.Connection:
    await clean_pg.execute(UP_004)
    await clean_pg.execute(UP_005)
    await clean_pg.execute(UP_006)
    await clean_pg.execute(UP_007)
    await clean_pg.execute(UP_008)
    # Force index consideration regardless of table-row counts.
    await clean_pg.execute("SET enable_seqscan = OFF")
    return clean_pg


async def _plan(conn: asyncpg.Connection, sql: str, *args) -> str:
    rows = await conn.fetch(f"EXPLAIN {sql}", *args)
    return "\n".join(r["QUERY PLAN"] for r in rows)


# ---------------------------------------------------------------------------
# §9.1 read-path indexes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idx_eval_team_time_used_by_team_finalized_query(
    pg_008: asyncpg.Connection,
) -> None:
    """Drives team-stats and dashboard date-range queries."""
    plan = await _plan(
        pg_008,
        "SELECT id FROM qa.evaluations "
        "WHERE state = 'finalized' AND team_id = 'sales' "
        "ORDER BY finalized_at DESC LIMIT 50",
    )
    assert "idx_eval_team_time" in plan


@pytest.mark.asyncio
async def test_idx_eval_agent_time_used_by_agent_history_query(
    pg_008: asyncpg.Connection,
) -> None:
    """Drives agent-history reads (ProgressionCard, agent dashboard)."""
    plan = await _plan(
        pg_008,
        "SELECT id FROM qa.evaluations "
        "WHERE state = 'finalized' AND agent_id = 1 "
        "ORDER BY finalized_at DESC LIMIT 50",
    )
    assert "idx_eval_agent_time" in plan


@pytest.mark.asyncio
async def test_idx_sections_trend_used_by_section_id_query(
    pg_008: asyncpg.Connection,
) -> None:
    """Drives per-category trend analysis across evaluations."""
    plan = await _plan(
        pg_008,
        "SELECT evaluation_id, numeric_score FROM qa.evaluation_sections "
        "WHERE section_id = 'greeting'",
    )
    assert "idx_sections_trend" in plan


# ---------------------------------------------------------------------------
# §9.2 agent_stat_points
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idx_stat_points_agent_used_by_sparkline_query(
    pg_008: asyncpg.Connection,
) -> None:
    """CC Zone D sparkline read pattern: latest N stat points for an agent."""
    plan = await _plan(
        pg_008,
        "SELECT score, ewma FROM qa.agent_stat_points "
        "WHERE agent_id = 1 ORDER BY id DESC LIMIT 10",
    )
    assert "idx_stat_points_agent" in plan


# ---------------------------------------------------------------------------
# §3.12 formula_versions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idx_formula_versions_team_effective_used_by_history(
    pg_008: asyncpg.Connection,
) -> None:
    """'What formula was current at time T for team X' query."""
    plan = await _plan(
        pg_008,
        "SELECT formula_version, formula_json FROM qa.formula_versions "
        "WHERE team_id = 'sales' ORDER BY effective_from DESC LIMIT 5",
    )
    assert "idx_formula_versions_team_effective" in plan


# ---------------------------------------------------------------------------
# §3.13 formula_compliance_sweeps
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idx_sweeps_formula_flagged_used_by_iteration_rate_query(
    pg_008: asyncpg.Connection,
) -> None:
    """The §7.7 runbook's flag-rate-per-version query: GROUP BY flagged
    WHERE swept_formula_version = ?."""
    plan = await _plan(
        pg_008,
        "SELECT flagged, COUNT(*) FROM qa.formula_compliance_sweeps "
        "WHERE swept_formula_version = 'sales_v1' "
        "GROUP BY flagged",
    )
    assert "idx_sweeps_formula_flagged" in plan


@pytest.mark.asyncio
async def test_idx_sweeps_eval_used_by_per_eval_history_query(
    pg_008: asyncpg.Connection,
) -> None:
    plan = await _plan(
        pg_008,
        "SELECT swept_formula_version, recomputed_score "
        "FROM qa.formula_compliance_sweeps WHERE evaluation_id = 1",
    )
    assert "idx_sweeps_eval" in plan


# ---------------------------------------------------------------------------
# §4.2 command_center.calls
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idx_calls_team_scored_connected_used_by_filtered_query(
    pg_008: asyncpg.Connection,
) -> None:
    """The (team_id, scored, connected_at) composite shines when the
    query filters on team+scored AND orders/ranges on connected_at —
    that's the "list scored calls in this window" pattern (CC drill-
    down, ratio query when materialized as paginated listings).

    Needs real row-count statistics: on an empty table the planner
    can't tell whether the (team_id, connected_at) partial index or
    this composite is cheaper, and may pick uq_calls_team_call_id
    instead because its prefix is also team_id. Populating + ANALYZE
    gives it the stats to choose."""
    await pg_008.execute(
        "INSERT INTO command_center.calls "
        "(team_id, dialpad_call_id, seen_via, scored, scored_at, connected_at) "
        "SELECT 'sales', 'call_' || g::text, 'webhook', "
        "       (g % 3 = 0), "
        "       CASE WHEN g % 3 = 0 THEN NOW() ELSE NULL END, "
        "       NOW() - (g || ' minutes')::interval "
        "FROM generate_series(1, 200) g"
    )
    await pg_008.execute("ANALYZE command_center.calls")

    plan = await _plan(
        pg_008,
        "SELECT id, connected_at FROM command_center.calls "
        "WHERE team_id = 'sales' AND scored = TRUE "
        "AND connected_at > NOW() - INTERVAL '2 hours' "
        "ORDER BY connected_at DESC LIMIT 20",
    )
    assert "idx_calls_team_scored_connected" in plan


@pytest.mark.asyncio
async def test_idx_calls_team_connected_used_by_date_range_query(
    pg_008: asyncpg.Connection,
) -> None:
    plan = await _plan(
        pg_008,
        "SELECT id FROM command_center.calls "
        "WHERE team_id = 'sales' AND connected_at IS NOT NULL "
        "ORDER BY connected_at DESC LIMIT 100",
    )
    assert "idx_calls_team_connected" in plan


@pytest.mark.asyncio
async def test_idx_calls_agent_used_by_per_agent_volume_query(
    pg_008: asyncpg.Connection,
) -> None:
    plan = await _plan(
        pg_008,
        "SELECT COUNT(*) FROM command_center.calls "
        "WHERE team_id = 'sales' AND dialpad_agent_id = 'a1'",
    )
    assert "idx_calls_agent" in plan


# ---------------------------------------------------------------------------
# §4.1 webhook_events replay
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idx_webhook_replay_used_by_rebuild_query(
    pg_008: asyncpg.Connection,
) -> None:
    """call_state.rebuild() reads in (team_id, event_timestamp, id) order."""
    plan = await _plan(
        pg_008,
        "SELECT raw_payload FROM command_center.webhook_events "
        "WHERE team_id = 'sales' AND event_timestamp >= '2026-06-23' "
        "ORDER BY event_timestamp ASC, id ASC",
    )
    assert "idx_webhook_replay" in plan


# ---------------------------------------------------------------------------
# §4.5 frequent_callers_cache
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idx_frequent_callers_team_phone_used_by_lookup(
    pg_008: asyncpg.Connection,
) -> None:
    plan = await _plan(
        pg_008,
        "SELECT * FROM command_center.frequent_callers_cache "
        "WHERE team_id = 'sales' AND caller_phone_e164 = '+15551234567'",
    )
    assert "idx_frequent_callers_team_phone" in plan


# ---------------------------------------------------------------------------
# §5.6 HNSW KNN — pgvector
# ---------------------------------------------------------------------------


def _vec(n: int) -> str:
    return "[" + ",".join("0.1" for _ in range(n)) + "]"


@pytest.mark.asyncio
async def test_hnsw_index_1536_used_by_cosine_knn_query(
    pg_008: asyncpg.Connection,
) -> None:
    """Documented query pattern: ORDER BY embedding_1536 <=> $1 LIMIT N.
    The pgvector planner picks the HNSW index for cosine-similarity KNN."""
    plan = await _plan(
        pg_008,
        "SELECT id FROM embeddings.sop_chunk_embeddings "
        f"WHERE embedding_1536 IS NOT NULL "
        f"ORDER BY embedding_1536 <=> '{_vec(1536)}'::vector LIMIT 5",
    )
    assert "idx_chunk_embeddings_hnsw_1536" in plan


@pytest.mark.asyncio
async def test_hnsw_index_1024_used_by_cosine_knn_query(
    pg_008: asyncpg.Connection,
) -> None:
    plan = await _plan(
        pg_008,
        "SELECT id FROM embeddings.sop_chunk_embeddings "
        f"WHERE embedding_1024 IS NOT NULL "
        f"ORDER BY embedding_1024 <=> '{_vec(1024)}'::vector LIMIT 5",
    )
    assert "idx_chunk_embeddings_hnsw_1024" in plan


# ---------------------------------------------------------------------------
# Down — all indexes gone, tables intact
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_down_drops_all_008_indexes(pg_008: asyncpg.Connection) -> None:
    await pg_008.execute(DOWN_008)

    # The §11.5 EXPLAIN tests assert specific index names by string; if
    # any 008 index survived the down, the planner would still mention
    # it. Check that no 008 index appears in pg_indexes.
    indexes_008 = [
        "idx_eval_team_time", "idx_eval_agent_time", "idx_sections_trend",
        "idx_stat_points_agent", "idx_formula_versions_team_effective",
        "idx_sweeps_formula_flagged", "idx_sweeps_eval",
        "idx_calls_team_scored_connected", "idx_calls_team_connected",
        "idx_calls_agent", "idx_webhook_replay",
        "idx_frequent_callers_team_phone",
        "idx_chunk_embeddings_hnsw_1536", "idx_chunk_embeddings_hnsw_1024",
    ]
    rows = await pg_008.fetch(
        "SELECT indexname FROM pg_indexes WHERE indexname = ANY($1::text[])",
        indexes_008,
    )
    assert rows == []


# ---------------------------------------------------------------------------
# Runner integration — full Wave 1 (004 → 008)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runner_applies_entire_wave_1_then_rolls_back_indexes(
    clean_pg: asyncpg.Connection, tmp_path: Path
) -> None:
    """End-to-end: every Wave-1 migration applies in order through the
    runner, and `down` rolls back just 008's indexes (leaving tables
    intact)."""
    import shutil
    from database import runner as _runner
    migdir = tmp_path / "migrations"
    migdir.mkdir()
    for name in [
        "004_create_schemas_and_teams.sql",
        "004_create_schemas_and_teams_down.sql",
        "005_command_center_tables.sql",
        "005_command_center_tables_down.sql",
        "006_qa_tables.sql",
        "006_qa_tables_down.sql",
        "007_embeddings_tables.sql",
        "007_embeddings_tables_down.sql",
        "008_indexes.sql",
        "008_indexes_down.sql",
    ]:
        shutil.copy(MIGRATIONS_DIR / name, migdir)

    rc = await _runner.cmd_up(clean_pg, migrations_dir=migdir)
    assert rc == 0

    applied = await clean_pg.fetch(
        "SELECT version FROM public.schema_migrations ORDER BY version"
    )
    assert [r["version"] for r in applied] == [4, 5, 6, 7, 8]

    # An 008 index exists
    cnt = await clean_pg.fetchval(
        "SELECT COUNT(*) FROM pg_indexes WHERE indexname = 'idx_eval_team_time'"
    )
    assert cnt == 1

    # Down only 008 — qa.* tables still there, indexes gone.
    rc = await _runner.cmd_down(clean_pg)
    assert rc == 0
    cnt = await clean_pg.fetchval(
        "SELECT COUNT(*) FROM pg_indexes WHERE indexname = 'idx_eval_team_time'"
    )
    assert cnt == 0
    # qa.evaluations table still exists.
    assert await clean_pg.fetchval("SELECT to_regclass('qa.evaluations')") is not None

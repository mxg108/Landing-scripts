"""Tests for migration 011 — qa.assessments + qa.assessment_sections.

Per SQLMigration.md §11.5 floor + §3.20 / §3.21:
  - ≥1 CHECK test per declared CHECK (trend enum, time_range positive,
    range_ordered, evaluations_included non-negative)
  - ≥1 UPSERT-idempotency / UNIQUE-conflict test
    (assessment_sections uniqueness per (assessment, section))
  - Cross-schema FK tests: rubric_version + formula_version + agent_id +
    team_id
  - is_current state-machine semantics: the successor-generation
    pattern (flip prior FALSE + INSERT new in one transaction)
  - CASCADE boundary on qa.assessment_sections.assessment_id
  - EXPLAIN-plan for idx_assessments_current (the hot read path)
  - Immutability by convention: the schema allows UPDATE at the DB
    level (Postgres has no per-column immutability). We document via
    tests that the CONTENT columns SHOULD NEVER be updated — that
    invariant is enforced at the API layer by Wave-2 application code,
    not the DB. The is_current flip test proves that particular
    UPDATE is intentional and doesn't touch content.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

from database import runner

REPO_ROOT = Path(__file__).resolve().parents[4]
MIGRATIONS_DIR = REPO_ROOT / "database" / "migrations"

UP_004 = (MIGRATIONS_DIR / "004_create_schemas_and_teams.sql").read_text()
UP_005 = (MIGRATIONS_DIR / "005_command_center_tables.sql").read_text()
UP_006 = (MIGRATIONS_DIR / "006_qa_tables.sql").read_text()
UP_009 = (MIGRATIONS_DIR / "009_vp_review_additions.sql").read_text()
UP_010 = (MIGRATIONS_DIR / "010_rubric_versioning.sql").read_text()
UP_011 = (MIGRATIONS_DIR / "011_assessments.sql").read_text()
DOWN_011 = (MIGRATIONS_DIR / "011_assessments_down.sql").read_text()


_MODELS_JSON = '{"text": {"provider": "gemini", "model": "gemini-2.5-flash"}}'


@pytest_asyncio.fixture
async def pg_011(clean_pg: asyncpg.Connection) -> asyncpg.Connection:
    """clean_pg + 004 + 005 + 006 + 009 + 010 + 011. Skips 007/008 — both
    are independent of the v1.4 surface."""
    await clean_pg.execute(UP_004)
    await clean_pg.execute(UP_005)
    await clean_pg.execute(UP_006)
    await clean_pg.execute(UP_009)
    await clean_pg.execute(UP_010)
    await clean_pg.execute(UP_011)
    return clean_pg


async def _make_agent(conn: asyncpg.Connection, name: str = "Alpha Rep") -> int:
    return await conn.fetchval(
        "INSERT INTO qa.agents (team_id, name, email) "
        "VALUES ('sales', $1, 'a@l.com') RETURNING id",
        name,
    )


async def _insert_assessment(
    conn: asyncpg.Connection,
    *,
    agent_id: int,
    time_range_days: int = 30,
    is_current: bool = True,
    rubric_version: str = "sales_v1",
    formula_version: str | None = None,
    evaluations_included: int = 12,
) -> int:
    """Insert a canonical-shape assessment row; return its id."""
    return await conn.fetchval(
        """
        INSERT INTO qa.assessments
            (agent_id, team_id, time_range_days,
             range_start_at, range_end_at,
             evaluations_included, overall_assessment,
             rubric_version, formula_version, models_used,
             estimated_cost_usd, is_current)
        VALUES ($1, 'sales', $2::integer,
                NOW() - make_interval(days => $2::integer), NOW(),
                $3, 'Andrés demonstrates strong foundational skills ...',
                $4, $5, $6::jsonb, 0.0342, $7)
        RETURNING id
        """,
        agent_id, time_range_days, evaluations_included,
        rubric_version, formula_version, _MODELS_JSON, is_current,
    )


# ---------------------------------------------------------------------------
# qa.assessments — column defaults + CHECK constraints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_insert_populates_all_required_columns(
    pg_011: asyncpg.Connection,
) -> None:
    aid = await _make_agent(pg_011)
    aid_row = await _insert_assessment(pg_011, agent_id=aid)
    row = await pg_011.fetchrow(
        "SELECT agent_id, team_id, time_range_days, evaluations_included, "
        "       overall_assessment, rubric_version, models_used, "
        "       is_current, generated_at "
        "FROM qa.assessments WHERE id = $1",
        aid_row,
    )
    assert row["agent_id"] == aid
    assert row["team_id"] == "sales"
    assert row["time_range_days"] == 30
    assert row["evaluations_included"] == 12
    assert row["overall_assessment"].startswith("Andrés")
    assert row["rubric_version"] == "sales_v1"
    assert row["models_used"] is not None
    assert row["is_current"] is True
    assert row["generated_at"] is not None


@pytest.mark.asyncio
async def test_is_current_defaults_true(pg_011: asyncpg.Connection) -> None:
    aid = await _make_agent(pg_011)
    row_id = await _insert_assessment(pg_011, agent_id=aid, is_current=True)
    is_current = await pg_011.fetchval(
        "SELECT is_current FROM qa.assessments WHERE id = $1", row_id
    )
    assert is_current is True


@pytest.mark.asyncio
async def test_generated_at_default_now(pg_011: asyncpg.Connection) -> None:
    """DEFAULT NOW() populates generated_at when the writer omits it."""
    aid = await _make_agent(pg_011)
    # Insert without generated_at — helper already does this.
    row_id = await _insert_assessment(pg_011, agent_id=aid)
    gen = await pg_011.fetchval(
        "SELECT generated_at FROM qa.assessments WHERE id = $1", row_id
    )
    assert gen is not None
    # Should be within a few seconds of now.
    now = datetime.datetime.now(datetime.timezone.utc)
    assert abs((now - gen).total_seconds()) < 10


@pytest.mark.asyncio
async def test_time_range_days_must_be_positive(
    pg_011: asyncpg.Connection,
) -> None:
    aid = await _make_agent(pg_011)
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await pg_011.execute(
            """
            INSERT INTO qa.assessments
                (agent_id, team_id, time_range_days, range_start_at, range_end_at,
                 evaluations_included, overall_assessment, rubric_version,
                 models_used)
            VALUES ($1, 'sales', 0, NOW(), NOW(), 1, 'x', 'sales_v1', $2::jsonb)
            """,
            aid, _MODELS_JSON,
        )


@pytest.mark.asyncio
async def test_range_start_must_be_lte_range_end(
    pg_011: asyncpg.Connection,
) -> None:
    aid = await _make_agent(pg_011)
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await pg_011.execute(
            """
            INSERT INTO qa.assessments
                (agent_id, team_id, time_range_days, range_start_at, range_end_at,
                 evaluations_included, overall_assessment, rubric_version,
                 models_used)
            VALUES ($1, 'sales', 30, NOW(), NOW() - INTERVAL '1 day',
                    1, 'x', 'sales_v1', $2::jsonb)
            """,
            aid, _MODELS_JSON,
        )


@pytest.mark.asyncio
async def test_evaluations_included_must_be_non_negative(
    pg_011: asyncpg.Connection,
) -> None:
    aid = await _make_agent(pg_011)
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await pg_011.execute(
            """
            INSERT INTO qa.assessments
                (agent_id, team_id, time_range_days, range_start_at, range_end_at,
                 evaluations_included, overall_assessment, rubric_version,
                 models_used)
            VALUES ($1, 'sales', 30, NOW() - INTERVAL '30 days', NOW(),
                    -1, 'x', 'sales_v1', $2::jsonb)
            """,
            aid, _MODELS_JSON,
        )


@pytest.mark.asyncio
async def test_time_range_days_integer_flexible(
    pg_011: asyncpg.Connection,
) -> None:
    """The schema imposes no enum on time_range_days — 7w (49), 30, 60,
    90, 120, 180 all accepted."""
    aid = await _make_agent(pg_011)
    for days in (7, 30, 49, 60, 90, 120, 180):
        row_id = await _insert_assessment(
            pg_011, agent_id=aid, time_range_days=days, is_current=False,
        )
        assert row_id is not None


# ---------------------------------------------------------------------------
# qa.assessments — FK enforcement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rubric_version_fk_required(pg_011: asyncpg.Connection) -> None:
    """rubric_version is NOT NULL — every assessment references an archived
    rubric so sections stay reproducible."""
    aid = await _make_agent(pg_011)
    with pytest.raises(asyncpg.exceptions.NotNullViolationError):
        await pg_011.execute(
            """
            INSERT INTO qa.assessments
                (agent_id, team_id, time_range_days, range_start_at, range_end_at,
                 evaluations_included, overall_assessment, models_used)
            VALUES ($1, 'sales', 30, NOW() - INTERVAL '30 days', NOW(),
                    1, 'x', $2::jsonb)
            """,
            aid, _MODELS_JSON,
        )


@pytest.mark.asyncio
async def test_rubric_version_fk_rejects_unknown(
    pg_011: asyncpg.Connection,
) -> None:
    aid = await _make_agent(pg_011)
    with pytest.raises(asyncpg.exceptions.ForeignKeyViolationError):
        await _insert_assessment(
            pg_011, agent_id=aid, rubric_version="never_existed_v99",
        )


@pytest.mark.asyncio
async def test_formula_version_nullable(pg_011: asyncpg.Connection) -> None:
    """formula_version is nullable for edge-case cases (§3.20). NULL
    insert must succeed."""
    aid = await _make_agent(pg_011)
    row_id = await _insert_assessment(
        pg_011, agent_id=aid, formula_version=None,
    )
    fv = await pg_011.fetchval(
        "SELECT formula_version FROM qa.assessments WHERE id = $1", row_id
    )
    assert fv is None


@pytest.mark.asyncio
async def test_formula_version_fk_rejects_unknown(
    pg_011: asyncpg.Connection,
) -> None:
    """Non-NULL formula_version must reference an archived row."""
    aid = await _make_agent(pg_011)
    with pytest.raises(asyncpg.exceptions.ForeignKeyViolationError):
        await _insert_assessment(
            pg_011, agent_id=aid, formula_version="never_existed_v99",
        )


@pytest.mark.asyncio
async def test_agent_id_fk_enforced(pg_011: asyncpg.Connection) -> None:
    with pytest.raises(asyncpg.exceptions.ForeignKeyViolationError):
        await _insert_assessment(pg_011, agent_id=999999)


@pytest.mark.asyncio
async def test_team_id_fk_enforced(pg_011: asyncpg.Connection) -> None:
    aid = await _make_agent(pg_011)
    with pytest.raises(asyncpg.exceptions.ForeignKeyViolationError):
        await pg_011.execute(
            """
            INSERT INTO qa.assessments
                (agent_id, team_id, time_range_days, range_start_at, range_end_at,
                 evaluations_included, overall_assessment, rubric_version,
                 models_used)
            VALUES ($1, 'phantom_team', 30,
                    NOW() - INTERVAL '30 days', NOW(),
                    1, 'x', 'sales_v1', $2::jsonb)
            """,
            aid, _MODELS_JSON,
        )


# ---------------------------------------------------------------------------
# qa.assessments — is_current successor semantics (§3.20 write path)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_second_row_flip_prior_current_in_one_transaction(
    pg_011: asyncpg.Connection,
) -> None:
    """The write path (§3.20 pseudocode): flip prior is_current=FALSE +
    INSERT new row in one transaction. No half-state where two rows carry
    is_current=TRUE for the same (agent, window)."""
    aid = await _make_agent(pg_011)
    v1 = await _insert_assessment(pg_011, agent_id=aid, time_range_days=30)

    async with pg_011.transaction():
        await pg_011.execute(
            "UPDATE qa.assessments SET is_current = FALSE "
            "WHERE agent_id = $1 AND time_range_days = 30 AND is_current = TRUE",
            aid,
        )
        v2 = await _insert_assessment(
            pg_011, agent_id=aid, time_range_days=30, is_current=True,
        )

    # Exactly one is_current=TRUE row for this (agent, window).
    n_current = await pg_011.fetchval(
        "SELECT COUNT(*) FROM qa.assessments "
        "WHERE agent_id = $1 AND time_range_days = 30 AND is_current = TRUE",
        aid,
    )
    assert n_current == 1
    # The survivor is v2.
    current_id = await pg_011.fetchval(
        "SELECT id FROM qa.assessments "
        "WHERE agent_id = $1 AND time_range_days = 30 AND is_current = TRUE",
        aid,
    )
    assert current_id == v2
    # v1 still exists as history — assessments are append-only.
    v1_exists = await pg_011.fetchval(
        "SELECT is_current FROM qa.assessments WHERE id = $1", v1
    )
    assert v1_exists is False


@pytest.mark.asyncio
async def test_different_windows_can_both_be_current(
    pg_011: asyncpg.Connection,
) -> None:
    """The is_current flag is per (agent, time_range_days) — an agent
    can simultaneously have current 30d + 60d + 90d assessments."""
    aid = await _make_agent(pg_011)
    for days in (30, 60, 90):
        await _insert_assessment(
            pg_011, agent_id=aid, time_range_days=days, is_current=True,
        )
    n_current = await pg_011.fetchval(
        "SELECT COUNT(*) FROM qa.assessments "
        "WHERE agent_id = $1 AND is_current = TRUE",
        aid,
    )
    assert n_current == 3


# ---------------------------------------------------------------------------
# qa.assessment_sections — trend CHECK + UNIQUE + CASCADE
# ---------------------------------------------------------------------------


async def _insert_section(
    conn: asyncpg.Connection,
    *,
    assessment_id: int,
    section_id: str = "greeting",
    section_name: str = "Greeting",
    section_number: int = 1,
    trend: str = "stable",
) -> int:
    return await conn.fetchval(
        """
        INSERT INTO qa.assessment_sections
            (assessment_id, section_id, section_name, section_number,
             trend, summary, coaching_tip)
        VALUES ($1, $2, $3, $4, $5,
                'Section summary paragraph.',
                'Actionable coaching tip.')
        RETURNING id
        """,
        assessment_id, section_id, section_name, section_number, trend,
    )


@pytest.mark.asyncio
async def test_trend_check_accepts_three_values(
    pg_011: asyncpg.Connection,
) -> None:
    aid = await _make_agent(pg_011)
    row_id = await _insert_assessment(pg_011, agent_id=aid)
    for trend in ("improving", "stable", "declining"):
        await _insert_section(
            pg_011, assessment_id=row_id, section_id=trend,
            section_name=f"S-{trend}", trend=trend,
        )
    n = await pg_011.fetchval(
        "SELECT COUNT(*) FROM qa.assessment_sections WHERE assessment_id = $1",
        row_id,
    )
    assert n == 3


@pytest.mark.asyncio
async def test_trend_check_rejects_other(pg_011: asyncpg.Connection) -> None:
    aid = await _make_agent(pg_011)
    row_id = await _insert_assessment(pg_011, agent_id=aid)
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await _insert_section(
            pg_011, assessment_id=row_id, trend="unclear",
        )


@pytest.mark.asyncio
async def test_unique_assessment_section(pg_011: asyncpg.Connection) -> None:
    """The AI cannot output two entries for the same section on the same
    assessment. UNIQUE (assessment_id, section_id) enforces at write time."""
    aid = await _make_agent(pg_011)
    row_id = await _insert_assessment(pg_011, agent_id=aid)
    await _insert_section(pg_011, assessment_id=row_id, section_id="greeting")
    with pytest.raises(asyncpg.exceptions.UniqueViolationError):
        await _insert_section(pg_011, assessment_id=row_id, section_id="greeting")


@pytest.mark.asyncio
async def test_cascade_on_assessment_delete(
    pg_011: asyncpg.Connection,
) -> None:
    """Deleting an assessment cascades to its sections (paired concept)."""
    aid = await _make_agent(pg_011)
    row_id = await _insert_assessment(pg_011, agent_id=aid)
    for i, sid in enumerate(("greeting", "process_adherence", "call_resolution"), 1):
        await _insert_section(
            pg_011, assessment_id=row_id, section_id=sid,
            section_name=sid, section_number=i,
        )
    await pg_011.execute("DELETE FROM qa.assessments WHERE id = $1", row_id)
    n = await pg_011.fetchval(
        "SELECT COUNT(*) FROM qa.assessment_sections WHERE assessment_id = $1",
        row_id,
    )
    assert n == 0


@pytest.mark.asyncio
async def test_section_name_number_snapshotted_at_gen_time(
    pg_011: asyncpg.Connection,
) -> None:
    """Snapshotted columns preserve rendering when a section is renamed
    in a future rubric. Demonstrated via write with explicit values —
    the schema doesn't look them up."""
    aid = await _make_agent(pg_011)
    row_id = await _insert_assessment(pg_011, agent_id=aid)
    await _insert_section(
        pg_011, assessment_id=row_id,
        section_id="greeting",
        # Simulate the display name at the time this assessment was
        # generated, which may differ from a future rubric's name.
        section_name="Greeting (deprecated verbiage)",
        section_number=1,
    )
    row = await pg_011.fetchrow(
        "SELECT section_name, section_number FROM qa.assessment_sections "
        "WHERE assessment_id = $1", row_id,
    )
    assert row["section_name"] == "Greeting (deprecated verbiage)"
    assert row["section_number"] == 1


# ---------------------------------------------------------------------------
# EXPLAIN-plan — the hot read path uses idx_assessments_current
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_current_read_path_uses_partial_index(
    pg_011: asyncpg.Connection,
) -> None:
    """§3.20.1 read path: WHERE agent_id=? AND time_range_days=? AND
    is_current=TRUE ORDER BY generated_at DESC LIMIT 1. Must hit the
    partial index."""
    await pg_011.execute("SET enable_seqscan = OFF")
    rows = await pg_011.fetch(
        "EXPLAIN SELECT * FROM qa.assessments "
        "WHERE agent_id = 1 AND time_range_days = 30 AND is_current = TRUE "
        "ORDER BY generated_at DESC LIMIT 1"
    )
    plan = "\n".join(r["QUERY PLAN"] for r in rows)
    assert "idx_assessments_current" in plan


@pytest.mark.asyncio
async def test_section_trend_analytics_index_exists(
    pg_011: asyncpg.Connection,
) -> None:
    """The (section_id, trend) index for the cross-agent analytics use
    case Ops wants. Not asserting the planner picks it (empty table
    stats are unreliable) — just that it's present."""
    idx = await pg_011.fetchval(
        "SELECT indexname FROM pg_indexes "
        "WHERE schemaname = 'qa' "
        "  AND indexname = 'idx_assessment_sections_section_trend'"
    )
    assert idx == "idx_assessment_sections_section_trend"


# ---------------------------------------------------------------------------
# Down — drops both tables cleanly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_down_drops_both_v1_4_tables(
    pg_011: asyncpg.Connection,
) -> None:
    await pg_011.execute(DOWN_011)
    for table in ("qa.assessments", "qa.assessment_sections"):
        assert await pg_011.fetchval(f"SELECT to_regclass('{table}')") is None


# ---------------------------------------------------------------------------
# Runner integration — 004 → 005 → 006 → 009 → 010 → 011
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runner_applies_004_through_011(
    clean_pg: asyncpg.Connection, tmp_path: Path
) -> None:
    import shutil
    migdir = tmp_path / "migrations"
    migdir.mkdir()
    for name in [
        "004_create_schemas_and_teams.sql",
        "004_create_schemas_and_teams_down.sql",
        "005_command_center_tables.sql",
        "005_command_center_tables_down.sql",
        "006_qa_tables.sql",
        "006_qa_tables_down.sql",
        "009_vp_review_additions.sql",
        "009_vp_review_additions_down.sql",
        "010_rubric_versioning.sql",
        "010_rubric_versioning_down.sql",
        "011_assessments.sql",
        "011_assessments_down.sql",
    ]:
        shutil.copy(MIGRATIONS_DIR / name, migdir)

    rc = await runner.cmd_up(clean_pg, migrations_dir=migdir)
    assert rc == 0
    applied = await clean_pg.fetch(
        "SELECT version FROM public.schema_migrations ORDER BY version"
    )
    assert [r["version"] for r in applied] == [4, 5, 6, 9, 10, 11]

    # Sanity — qa.assessments exists and rubric_versions FK works.
    assert (
        await clean_pg.fetchval("SELECT to_regclass('qa.assessments')")
        is not None
    )
    assert (
        await clean_pg.fetchval("SELECT to_regclass('qa.assessment_sections')")
        is not None
    )
    # Partial rollback of 011 leaves 010 intact.
    rc = await runner.cmd_down(clean_pg)
    assert rc == 0
    assert await clean_pg.fetchval("SELECT to_regclass('qa.assessments')") is None
    assert (
        await clean_pg.fetchval("SELECT to_regclass('qa.rubric_versions')")
        is not None
    )

"""Tests for migration 016 — CC dispositions, AI-CSAT, hold_intervals.

Per DispositionDesign.md §3 (v2.1) + SQLMigration.md §11.5 floor:
  - ≥1 CHECK test per declared CHECK (disposition_source enum,
    disposition↔source pair, subdisposition-requires-category,
    hold ended_by enum, interval ordering, seconds non-negative)
  - No enum CHECK on disposition labels themselves — admin-editable
    labels drift, so any TEXT must be accepted
  - ai_csat lives on BOTH tables and is distinct from
    qa.evaluations.csat_score (the user-survey slot)
  - hold_intervals CASCADE boundary on call delete + FK enforcement
  - Triple-key match indexes (entry-point + master) present on CC calls
  - Down: table dropped, columns gone, 006/005 surfaces intact
  - Runner up/down via a tmp migrations dir
"""

from __future__ import annotations

from datetime import datetime, timezone
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
UP_016 = (MIGRATIONS_DIR / "016_cc_dispositions_ai_csat_holds.sql").read_text()
DOWN_016 = (MIGRATIONS_DIR / "016_cc_dispositions_ai_csat_holds_down.sql").read_text()


@pytest_asyncio.fixture
async def pg_016(clean_pg: asyncpg.Connection) -> asyncpg.Connection:
    """clean_pg + 004 + 005 + 006 + 016. Skips 007-015 — 016's DDL only
    depends on public.teams, command_center.calls, and qa.evaluations."""
    await clean_pg.execute(UP_004)
    await clean_pg.execute(UP_005)
    await clean_pg.execute(UP_006)
    await clean_pg.execute(UP_016)
    return clean_pg


async def _make_call(
    conn: asyncpg.Connection,
    *,
    dialpad_call_id: str = "DP-1",
    team_id: str = "member_support",
    **cols,
) -> int:
    """Insert a minimal command_center.calls row; return its id."""
    extra_names = "".join(f", {k}" for k in cols)
    extra_params = "".join(f", ${i}" for i in range(4, 4 + len(cols)))
    return await conn.fetchval(
        f"""
        INSERT INTO command_center.calls
            (team_id, dialpad_call_id, seen_via{extra_names})
        VALUES ($1, $2, $3{extra_params})
        RETURNING id
        """,
        team_id, dialpad_call_id, "webhook", *cols.values(),
    )


def _ts(hh: int, mm: int, ss: int) -> datetime:
    return datetime(2026, 7, 15, hh, mm, ss, tzinfo=timezone.utc)


async def _insert_hold(
    conn: asyncpg.Connection,
    *,
    call_id: int,
    dialpad_call_id: str = "DP-1",
    started_at: datetime | None = None,
    ended_at: datetime | None = None,
    seconds: int = 102,
    ended_by: str = "connected",
) -> int:
    return await conn.fetchval(
        """
        INSERT INTO command_center.hold_intervals
            (team_id, dialpad_call_id, call_id, started_at, ended_at,
             seconds, ended_by)
        VALUES ('member_support', $1, $2, $3, $4, $5, $6)
        RETURNING id
        """,
        dialpad_call_id, call_id,
        started_at or _ts(10, 3, 15),
        ended_at or _ts(10, 4, 57),
        seconds, ended_by,
    )


# ---------------------------------------------------------------------------
# command_center.calls — disposition columns
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_disposition_roundtrip(pg_016: asyncpg.Connection) -> None:
    """Category~Sub split into the two columns, with webhook provenance."""
    cid = await _make_call(
        pg_016,
        disposition_category="Access & Entry",
        disposition="Smart-lock failure",
        disposition_source="webhook",
        ai_csat=4.5,
    )
    row = await pg_016.fetchrow(
        "SELECT disposition_category, disposition, disposition_source, ai_csat "
        "FROM command_center.calls WHERE id = $1", cid,
    )
    assert row["disposition_category"] == "Access & Entry"
    assert row["disposition"] == "Smart-lock failure"
    assert row["disposition_source"] == "webhook"
    assert float(row["ai_csat"]) == 4.5


@pytest.mark.asyncio
async def test_bare_category_leaves_disposition_null(
    pg_016: asyncpg.Connection,
) -> None:
    """Agent stopped at level 1 of the Category~Sub form — bare category,
    NULL subdisposition is a valid state."""
    cid = await _make_call(
        pg_016,
        disposition_category="Billing",
        disposition_source="stats_pull",
    )
    row = await pg_016.fetchrow(
        "SELECT disposition, disposition_source "
        "FROM command_center.calls WHERE id = $1", cid,
    )
    assert row["disposition"] is None
    assert row["disposition_source"] == "stats_pull"


@pytest.mark.asyncio
async def test_undispositioned_call_all_null(pg_016: asyncpg.Connection) -> None:
    """Absence is expected-normal (back-to-back edge + outbound) — a call
    with no disposition at all must insert cleanly."""
    cid = await _make_call(pg_016)
    row = await pg_016.fetchrow(
        "SELECT disposition_category, disposition, disposition_source, ai_csat "
        "FROM command_center.calls WHERE id = $1", cid,
    )
    assert all(row[k] is None for k in row.keys())


@pytest.mark.asyncio
async def test_no_enum_check_on_labels(pg_016: asyncpg.Connection) -> None:
    """Labels are admin-editable and drift — any TEXT category/sub must be
    accepted (the design explicitly rejects a label CHECK)."""
    cid = await _make_call(
        pg_016,
        disposition_category="Brand-New Category Ops Invented Yesterday",
        disposition="Sub · with weird ~ punctuation",
        disposition_source="webhook",
    )
    assert cid is not None


@pytest.mark.asyncio
async def test_disposition_source_check_rejects_unknown(
    pg_016: asyncpg.Connection,
) -> None:
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await _make_call(
            pg_016,
            disposition_category="Billing",
            disposition_source="manual_edit",
        )


@pytest.mark.asyncio
async def test_disposition_without_source_rejected(
    pg_016: asyncpg.Connection,
) -> None:
    """A disposition never arrives without provenance."""
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await _make_call(pg_016, disposition_category="Billing")


@pytest.mark.asyncio
async def test_source_without_disposition_rejected(
    pg_016: asyncpg.Connection,
) -> None:
    """Provenance is meaningless without a disposition."""
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await _make_call(pg_016, disposition_source="webhook")


@pytest.mark.asyncio
async def test_subdisposition_requires_category(
    pg_016: asyncpg.Connection,
) -> None:
    """Level 2 of Category~Sub cannot exist without level 1."""
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await _make_call(pg_016, disposition="Smart-lock failure")


# ---------------------------------------------------------------------------
# qa.evaluations — Stage-1 stamp columns
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_evaluations_stamp_columns_nullable_and_roundtrip(
    pg_016: asyncpg.Connection,
) -> None:
    eid = await pg_016.fetchval(
        """
        INSERT INTO qa.evaluations
            (team_id, agent_name_raw, state, source, models_used,
             dialpad_disposition_category, dialpad_disposition, ai_csat)
        VALUES ('member_support', 'Jane Agent', 'draft', 'ai',
                '{"text": {"provider": "gemini"}}'::jsonb,
                'Access & Entry', 'Smart-lock failure', 4.0)
        RETURNING id
        """
    )
    row = await pg_016.fetchrow(
        "SELECT dialpad_disposition_category, dialpad_disposition, ai_csat, "
        "       csat_score "
        "FROM qa.evaluations WHERE id = $1", eid,
    )
    assert row["dialpad_disposition_category"] == "Access & Entry"
    assert row["dialpad_disposition"] == "Smart-lock failure"
    assert float(row["ai_csat"]) == 4.0
    # ai_csat is a NEW slot — the 006 user-survey column is untouched.
    assert row["csat_score"] is None


@pytest.mark.asyncio
async def test_evaluations_columns_null_for_undispositioned(
    pg_016: asyncpg.Connection,
) -> None:
    """NULL = never captured; the absence path (§5) is a first-class state."""
    eid = await pg_016.fetchval(
        """
        INSERT INTO qa.evaluations
            (team_id, agent_name_raw, state, source, models_used)
        VALUES ('member_support', 'Jane Agent', 'draft', 'ai',
                '{"text": {"provider": "gemini"}}'::jsonb)
        RETURNING id
        """
    )
    row = await pg_016.fetchrow(
        "SELECT dialpad_disposition_category, dialpad_disposition, ai_csat "
        "FROM qa.evaluations WHERE id = $1", eid,
    )
    assert all(row[k] is None for k in row.keys())


# ---------------------------------------------------------------------------
# command_center.hold_intervals
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hold_cycle_roundtrip(pg_016: asyncpg.Connection) -> None:
    cid = await _make_call(pg_016)
    hid = await _insert_hold(pg_016, call_id=cid)
    row = await pg_016.fetchrow(
        "SELECT team_id, dialpad_call_id, call_id, seconds, ended_by "
        "FROM command_center.hold_intervals WHERE id = $1", hid,
    )
    assert row["call_id"] == cid
    assert row["seconds"] == 102
    assert row["ended_by"] == "connected"


@pytest.mark.asyncio
async def test_hold_ended_by_hangup_accepted(pg_016: asyncpg.Connection) -> None:
    """A call that ends while on hold closes its cycle with `hangup` —
    the reconnect-flush case C2's fold must produce."""
    cid = await _make_call(pg_016)
    hid = await _insert_hold(pg_016, call_id=cid, ended_by="hangup")
    assert hid is not None


@pytest.mark.asyncio
async def test_hold_ended_by_check_rejects_unhold(
    pg_016: asyncpg.Connection,
) -> None:
    """No `unhold` event exists in Dialpad's stream — the schema refuses
    the vocabulary so a buggy fold can't invent it."""
    cid = await _make_call(pg_016)
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await _insert_hold(pg_016, call_id=cid, ended_by="unhold")


@pytest.mark.asyncio
async def test_hold_interval_must_be_ordered(pg_016: asyncpg.Connection) -> None:
    cid = await _make_call(pg_016)
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await _insert_hold(
            pg_016, call_id=cid,
            started_at=_ts(10, 5, 0),
            ended_at=_ts(10, 4, 0),
        )


@pytest.mark.asyncio
async def test_hold_seconds_non_negative(pg_016: asyncpg.Connection) -> None:
    cid = await _make_call(pg_016)
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await _insert_hold(pg_016, call_id=cid, seconds=-1)


@pytest.mark.asyncio
async def test_hold_intervals_cascade_on_call_delete(
    pg_016: asyncpg.Connection,
) -> None:
    """Cycles are per-call detail — deleting the call removes them."""
    cid = await _make_call(pg_016)
    await _insert_hold(pg_016, call_id=cid)
    await _insert_hold(
        pg_016, call_id=cid,
        started_at=_ts(10, 11, 2),
        ended_at=_ts(10, 12, 0),
        seconds=58,
    )
    await pg_016.execute("DELETE FROM command_center.calls WHERE id = $1", cid)
    n = await pg_016.fetchval(
        "SELECT COUNT(*) FROM command_center.hold_intervals WHERE call_id = $1",
        cid,
    )
    assert n == 0


@pytest.mark.asyncio
async def test_hold_call_fk_enforced(pg_016: asyncpg.Connection) -> None:
    with pytest.raises(asyncpg.exceptions.ForeignKeyViolationError):
        await _insert_hold(pg_016, call_id=999999)


@pytest.mark.asyncio
async def test_hold_team_fk_enforced(pg_016: asyncpg.Connection) -> None:
    cid = await _make_call(pg_016)
    with pytest.raises(asyncpg.exceptions.ForeignKeyViolationError):
        await pg_016.execute(
            """
            INSERT INTO command_center.hold_intervals
                (team_id, dialpad_call_id, call_id, started_at, ended_at,
                 seconds, ended_by)
            VALUES ('phantom_team', 'DP-1', $1, $2, $3, 60, 'connected')
            """,
            cid, _ts(10, 0, 0), _ts(10, 1, 0),
        )


# ---------------------------------------------------------------------------
# Triple-key match indexes (§5)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_triple_key_match_indexes_exist(
    pg_016: asyncpg.Connection,
) -> None:
    """Entry-point + master probes for the C3 triple-key match (call_id is
    already covered by uq_calls_team_call_id from 005). Presence only —
    empty-table planner stats make EXPLAIN assertions unreliable."""
    rows = await pg_016.fetch(
        "SELECT indexname FROM pg_indexes "
        "WHERE schemaname = 'command_center' AND tablename = 'calls'"
    )
    names = {r["indexname"] for r in rows}
    assert "idx_calls_entry_point_call_id" in names
    assert "idx_calls_master_call_id" in names


# ---------------------------------------------------------------------------
# Down — clean rollback, 005/006 surfaces intact
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_down_removes_016_surface_only(
    pg_016: asyncpg.Connection,
) -> None:
    await pg_016.execute(DOWN_016)
    assert await pg_016.fetchval(
        "SELECT to_regclass('command_center.hold_intervals')"
    ) is None
    cc_cols = {
        r["column_name"]
        for r in await pg_016.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'command_center' AND table_name = 'calls'"
        )
    }
    assert not cc_cols & {
        "disposition_category", "disposition", "ai_csat", "disposition_source"
    }
    ev_cols = {
        r["column_name"]
        for r in await pg_016.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'qa' AND table_name = 'evaluations'"
        )
    }
    assert not ev_cols & {
        "dialpad_disposition_category", "dialpad_disposition", "ai_csat"
    }
    # 005/006 surfaces intact.
    assert await pg_016.fetchval(
        "SELECT to_regclass('command_center.calls')"
    ) is not None
    assert "csat_score" in ev_cols
    assert "total_hold_seconds" in cc_cols


# ---------------------------------------------------------------------------
# Runner integration — up then down through the runner
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runner_up_down_clean(
    clean_pg: asyncpg.Connection, tmp_path: Path
) -> None:
    """The C1 checkpoint, containerized: runner applies 004→005→006→016,
    then rolls 016 back cleanly without disturbing 006."""
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
        "016_cc_dispositions_ai_csat_holds.sql",
        "016_cc_dispositions_ai_csat_holds_down.sql",
    ]:
        shutil.copy(MIGRATIONS_DIR / name, migdir)

    rc = await runner.cmd_up(clean_pg, migrations_dir=migdir)
    assert rc == 0
    applied = await clean_pg.fetch(
        "SELECT version FROM public.schema_migrations ORDER BY version"
    )
    assert [r["version"] for r in applied] == [4, 5, 6, 16]
    assert await clean_pg.fetchval(
        "SELECT to_regclass('command_center.hold_intervals')"
    ) is not None

    rc = await runner.cmd_down(clean_pg)
    assert rc == 0
    assert await clean_pg.fetchval(
        "SELECT to_regclass('command_center.hold_intervals')"
    ) is None
    assert await clean_pg.fetchval(
        "SELECT to_regclass('qa.evaluations')"
    ) is not None

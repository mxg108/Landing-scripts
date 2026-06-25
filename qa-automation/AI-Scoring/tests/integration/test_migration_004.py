"""Tests for migration 004 — schema namespaces + public.teams + seed.

Per SQLMigration.md §11.5: ≥1 UPSERT-idempotency test (`teams.id` PK is the
conflict target); no CHECK tests required (`teams` declares none).

Also smoke-exercises the migration through the runner (database/runner.py)
so we have end-to-end evidence the file is reachable as 004 via the
runner's discovery, not just isolated SQL.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import asyncpg
import pytest

from database import runner

REPO_ROOT = Path(__file__).resolve().parents[3]
MIGRATIONS_DIR = REPO_ROOT / "database" / "migrations"
UP_SQL = (MIGRATIONS_DIR / "004_create_schemas_and_teams.sql").read_text()
DOWN_SQL = (MIGRATIONS_DIR / "004_create_schemas_and_teams_down.sql").read_text()


# ---------------------------------------------------------------------------
# SQL-level correctness — apply directly, assert state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_creates_qa_command_center_embeddings_schemas(
    clean_pg: asyncpg.Connection,
) -> None:
    await clean_pg.execute(UP_SQL)
    rows = await clean_pg.fetch(
        "SELECT schema_name FROM information_schema.schemata "
        "WHERE schema_name IN ('qa', 'command_center', 'embeddings')"
    )
    assert {r["schema_name"] for r in rows} == {"qa", "command_center", "embeddings"}


@pytest.mark.asyncio
async def test_installs_pgvector_extension(clean_pg: asyncpg.Connection) -> None:
    await clean_pg.execute(UP_SQL)
    ext_name = await clean_pg.fetchval(
        "SELECT extname FROM pg_extension WHERE extname = 'vector'"
    )
    assert ext_name == "vector"


@pytest.mark.asyncio
async def test_pgvector_is_usable_after_install(clean_pg: asyncpg.Connection) -> None:
    """Confirms the extension is loaded into a usable state, not just
    registered — VECTOR(N) is the type 006/007/008 will use."""
    await clean_pg.execute(UP_SQL)
    # Create a throwaway table with a vector column; the extension must
    # provide the VECTOR type for this to succeed.
    await clean_pg.execute("CREATE TABLE public.smoke_vec (v VECTOR(3))")
    await clean_pg.execute("INSERT INTO public.smoke_vec (v) VALUES ('[1,2,3]')")
    val = await clean_pg.fetchval("SELECT v::text FROM public.smoke_vec")
    assert val == "[1,2,3]"


@pytest.mark.asyncio
async def test_public_teams_seeded_with_member_support_and_sales(
    clean_pg: asyncpg.Connection,
) -> None:
    await clean_pg.execute(UP_SQL)
    rows = await clean_pg.fetch(
        "SELECT id, name, timezone, default_language, active "
        "FROM public.teams ORDER BY id"
    )
    seed = [(r["id"], r["name"], r["timezone"], r["default_language"], r["active"])
            for r in rows]
    assert seed == [
        ("member_support", "Member Support", "America/Mexico_City", "en", True),
        ("sales",          "Sales",          "America/Mexico_City", "en", True),
    ]


@pytest.mark.asyncio
async def test_teams_created_at_defaults_to_now(clean_pg: asyncpg.Connection) -> None:
    """`created_at` has a NOT NULL default — seed rows must populate it."""
    await clean_pg.execute(UP_SQL)
    created = await clean_pg.fetchval(
        "SELECT created_at FROM public.teams WHERE id = 'member_support'"
    )
    assert created is not None


# ---------------------------------------------------------------------------
# §11.5 UPSERT-idempotency floor — re-running the migration is a no-op
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_re_apply_is_idempotent_no_seed_duplication(
    clean_pg: asyncpg.Connection,
) -> None:
    """The up SQL uses `CREATE ... IF NOT EXISTS` for schemas/table and
    `ON CONFLICT (id) DO NOTHING` for the seed rows — re-applying the
    whole script against a database that already has it must not error
    or duplicate seed rows. This is what makes the migration safe to
    re-run in a partial-rollback recovery scenario."""
    await clean_pg.execute(UP_SQL)
    await clean_pg.execute(UP_SQL)  # second apply
    count = await clean_pg.fetchval("SELECT COUNT(*) FROM public.teams")
    assert count == 2


@pytest.mark.asyncio
async def test_manual_seed_edit_survives_reapply(
    clean_pg: asyncpg.Connection,
) -> None:
    """If an operator edits `active=FALSE` on a seed row directly, a
    re-apply of 004 must not overwrite it back to TRUE — the
    `ON CONFLICT DO NOTHING` is what preserves operator intent."""
    await clean_pg.execute(UP_SQL)
    await clean_pg.execute(
        "UPDATE public.teams SET active = FALSE WHERE id = 'sales'"
    )
    await clean_pg.execute(UP_SQL)  # re-apply must NOT re-flip `active`
    active = await clean_pg.fetchval(
        "SELECT active FROM public.teams WHERE id = 'sales'"
    )
    assert active is False


# ---------------------------------------------------------------------------
# Down — full teardown returns DB to empty
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_down_removes_schemas_extension_and_table(
    clean_pg: asyncpg.Connection,
) -> None:
    await clean_pg.execute(UP_SQL)
    await clean_pg.execute(DOWN_SQL)

    rows = await clean_pg.fetch(
        "SELECT schema_name FROM information_schema.schemata "
        "WHERE schema_name IN ('qa', 'command_center', 'embeddings')"
    )
    assert rows == []

    ext = await clean_pg.fetchval(
        "SELECT extname FROM pg_extension WHERE extname = 'vector'"
    )
    assert ext is None

    teams = await clean_pg.fetchval("SELECT to_regclass('public.teams')")
    assert teams is None


# ---------------------------------------------------------------------------
# Runner integration — file is discoverable + applies cleanly through the CLI
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runner_picks_up_004_and_applies(
    clean_pg: asyncpg.Connection, tmp_path: Path
) -> None:
    """Copies just the 004 pair into a temp dir, points the runner at it,
    runs cmd_up. Asserts on schema_migrations and on the resulting state.

    Why a temp dir: avoids dragging 001/002/003 along for the ride. The
    runner's discovery is path-agnostic; this proves 004's filename
    matches the expected `NNN_name.sql` pattern and applies cleanly via
    the runner's transaction wrapping."""
    migdir = tmp_path / "migrations"
    migdir.mkdir()
    shutil.copy(MIGRATIONS_DIR / "004_create_schemas_and_teams.sql", migdir)
    shutil.copy(MIGRATIONS_DIR / "004_create_schemas_and_teams_down.sql", migdir)

    rc = await runner.cmd_up(clean_pg, migrations_dir=migdir)
    assert rc == 0

    applied = await clean_pg.fetch(
        "SELECT version, name FROM public.schema_migrations"
    )
    assert [(r["version"], r["name"]) for r in applied] == [
        (4, "create_schemas_and_teams"),
    ]
    # Cross-check the migration actually executed.
    assert await clean_pg.fetchval("SELECT COUNT(*) FROM public.teams") == 2


@pytest.mark.asyncio
async def test_runner_can_roll_back_004(
    clean_pg: asyncpg.Connection, tmp_path: Path
) -> None:
    """End-to-end up → down → state-empty via the runner. Confirms the
    `_down.sql` companion is paired correctly and that down_sql is
    snapshotted into schema_migrations at apply time per the runner's
    contract."""
    migdir = tmp_path / "migrations"
    migdir.mkdir()
    shutil.copy(MIGRATIONS_DIR / "004_create_schemas_and_teams.sql", migdir)
    shutil.copy(MIGRATIONS_DIR / "004_create_schemas_and_teams_down.sql", migdir)

    await runner.cmd_up(clean_pg, migrations_dir=migdir)
    rc = await runner.cmd_down(clean_pg)
    assert rc == 0

    assert (
        await clean_pg.fetchval("SELECT COUNT(*) FROM public.schema_migrations")
        == 0
    )
    assert await clean_pg.fetchval("SELECT to_regclass('public.teams')") is None

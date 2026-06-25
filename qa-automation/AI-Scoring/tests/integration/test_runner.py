"""Tests for ``database/runner.py``.

These tests point the runner at a temp migrations directory rather than
``database/migrations/`` proper so they never touch real schema files and
stay deterministic regardless of disk state. The runner's discovery
function reads from a configurable directory, so tests pass it directly.

Pattern note: per SQLMigration.md §11.3, each test that mutates state
asserts on the resulting schema (``to_regclass``) rather than re-reading
SQL — that's the correctness gate, not the SQL itself.
"""

from __future__ import annotations

from pathlib import Path

import asyncpg
import pytest

from database import runner


# ---------------------------------------------------------------------------
# discover_migrations
# ---------------------------------------------------------------------------


def test_discover_pairs_up_with_down(tmp_path: Path) -> None:
    migdir = tmp_path / "migrations"
    migdir.mkdir()
    (migdir / "100_alpha.sql").write_text("SELECT 1;")
    (migdir / "100_alpha_down.sql").write_text("SELECT 2;")
    (migdir / "101_beta.sql").write_text("SELECT 3;")  # no down — irreversible

    found = runner.discover_migrations(migdir)
    assert [(m.version, m.name) for m in found] == [(100, "alpha"), (101, "beta")]
    assert found[0].down_path is not None
    assert found[1].down_path is None


def test_discover_ignores_non_sql_and_unparseable_names(tmp_path: Path) -> None:
    migdir = tmp_path / "migrations"
    migdir.mkdir()
    (migdir / "100_ok.sql").write_text("SELECT 1;")
    (migdir / "README.md").write_text("hi")
    (migdir / "not-a-migration.sql").write_text("SELECT 1;")
    (migdir / ".gitkeep").write_text("")

    found = runner.discover_migrations(migdir)
    assert [m.version for m in found] == [100]


# ---------------------------------------------------------------------------
# cmd_up — happy path, limit, idempotency, atomicity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_up_applies_pending_in_version_order(
    clean_pg: asyncpg.Connection, tmp_path: Path
) -> None:
    migdir = tmp_path / "migrations"
    migdir.mkdir()
    (migdir / "100_first.sql").write_text("CREATE TABLE public.alpha (id INT);")
    (migdir / "101_second.sql").write_text("CREATE TABLE public.beta (id INT);")

    rc = await runner.cmd_up(clean_pg, migrations_dir=migdir)
    assert rc == 0

    rows = await clean_pg.fetch(
        "SELECT version, name FROM public.schema_migrations ORDER BY version"
    )
    assert [(r["version"], r["name"]) for r in rows] == [
        (100, "first"),
        (101, "second"),
    ]
    assert await clean_pg.fetchval("SELECT to_regclass('public.alpha')") is not None
    assert await clean_pg.fetchval("SELECT to_regclass('public.beta')") is not None


@pytest.mark.asyncio
async def test_up_with_limit_applies_only_first_n(
    clean_pg: asyncpg.Connection, tmp_path: Path
) -> None:
    migdir = tmp_path / "migrations"
    migdir.mkdir()
    (migdir / "100_first.sql").write_text("CREATE TABLE public.alpha (id INT);")
    (migdir / "101_second.sql").write_text("CREATE TABLE public.beta (id INT);")

    rc = await runner.cmd_up(clean_pg, limit=1, migrations_dir=migdir)
    assert rc == 0

    rows = await clean_pg.fetch("SELECT version FROM public.schema_migrations")
    assert [r["version"] for r in rows] == [100]
    assert await clean_pg.fetchval("SELECT to_regclass('public.beta')") is None


@pytest.mark.asyncio
async def test_up_is_idempotent_when_nothing_pending(
    clean_pg: asyncpg.Connection, tmp_path: Path
) -> None:
    migdir = tmp_path / "migrations"
    migdir.mkdir()
    (migdir / "100_only.sql").write_text("CREATE TABLE public.x (id INT);")

    await runner.cmd_up(clean_pg, migrations_dir=migdir)
    # Second run is a no-op — same migration is already applied.
    rc = await runner.cmd_up(clean_pg, migrations_dir=migdir)
    assert rc == 0
    rows = await clean_pg.fetch("SELECT version FROM public.schema_migrations")
    assert [r["version"] for r in rows] == [100]


@pytest.mark.asyncio
async def test_up_rolls_back_failing_migration(
    clean_pg: asyncpg.Connection, tmp_path: Path
) -> None:
    """A failing migration leaves the DB in the prior state — no partial
    apply, no row in schema_migrations."""
    migdir = tmp_path / "migrations"
    migdir.mkdir()
    (migdir / "100_busted.sql").write_text(
        "CREATE TABLE public.before_fail (id INT); "
        "CREATE TABLE public.before_fail (id INT);"  # duplicate — fails
    )

    with pytest.raises(asyncpg.PostgresError):
        await runner.cmd_up(clean_pg, migrations_dir=migdir)

    rows = await clean_pg.fetch("SELECT version FROM public.schema_migrations")
    assert rows == []
    assert await clean_pg.fetchval("SELECT to_regclass('public.before_fail')") is None


@pytest.mark.asyncio
async def test_up_refuses_out_of_order(
    clean_pg: asyncpg.Connection, tmp_path: Path
) -> None:
    """If 100 is applied and 099 then appears on disk, ``up`` MUST refuse —
    silently applying 099 after 100 would violate the spec's ordering
    invariants (§7.6)."""
    migdir = tmp_path / "migrations"
    migdir.mkdir()
    (migdir / "100_high.sql").write_text("CREATE TABLE public.high (id INT);")

    rc = await runner.cmd_up(clean_pg, migrations_dir=migdir)
    assert rc == 0

    (migdir / "099_intruder.sql").write_text("CREATE TABLE public.intruder (id INT);")
    rc = await runner.cmd_up(clean_pg, migrations_dir=migdir)
    assert rc == 2
    assert await clean_pg.fetchval("SELECT to_regclass('public.intruder')") is None


# ---------------------------------------------------------------------------
# cmd_down — rollback, irreversible refusal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_down_rolls_back_most_recent(
    clean_pg: asyncpg.Connection, tmp_path: Path
) -> None:
    migdir = tmp_path / "migrations"
    migdir.mkdir()
    (migdir / "100_alpha.sql").write_text("CREATE TABLE public.alpha (id INT);")
    (migdir / "100_alpha_down.sql").write_text("DROP TABLE public.alpha;")
    (migdir / "101_beta.sql").write_text("CREATE TABLE public.beta (id INT);")
    (migdir / "101_beta_down.sql").write_text("DROP TABLE public.beta;")

    await runner.cmd_up(clean_pg, migrations_dir=migdir)
    rc = await runner.cmd_down(clean_pg)
    assert rc == 0

    rows = await clean_pg.fetch("SELECT version FROM public.schema_migrations")
    assert [r["version"] for r in rows] == [100]
    assert await clean_pg.fetchval("SELECT to_regclass('public.beta')") is None
    assert await clean_pg.fetchval("SELECT to_regclass('public.alpha')") is not None


@pytest.mark.asyncio
async def test_down_refuses_irreversible_migration(
    clean_pg: asyncpg.Connection, tmp_path: Path
) -> None:
    migdir = tmp_path / "migrations"
    migdir.mkdir()
    (migdir / "100_no_down.sql").write_text("CREATE TABLE public.x (id INT);")
    # No _down.sql — irreversible.

    await runner.cmd_up(clean_pg, migrations_dir=migdir)
    rc = await runner.cmd_down(clean_pg)
    assert rc == 2

    # The applied row is still there; nothing was rolled back.
    rows = await clean_pg.fetch("SELECT version FROM public.schema_migrations")
    assert [r["version"] for r in rows] == [100]
    assert await clean_pg.fetchval("SELECT to_regclass('public.x')") is not None


@pytest.mark.asyncio
async def test_down_survives_disk_deletion_via_snapshotted_sql(
    clean_pg: asyncpg.Connection, tmp_path: Path
) -> None:
    """The runner snapshots ``down_sql`` into ``schema_migrations`` at apply
    time. Deleting the ``_down.sql`` file later does NOT break rollback."""
    migdir = tmp_path / "migrations"
    migdir.mkdir()
    (migdir / "100_alpha.sql").write_text("CREATE TABLE public.alpha (id INT);")
    (migdir / "100_alpha_down.sql").write_text("DROP TABLE public.alpha;")

    await runner.cmd_up(clean_pg, migrations_dir=migdir)

    # Operator deletes the down file post-apply — runner should still roll back.
    (migdir / "100_alpha_down.sql").unlink()

    rc = await runner.cmd_down(clean_pg)
    assert rc == 0
    assert await clean_pg.fetchval("SELECT to_regclass('public.alpha')") is None


# ---------------------------------------------------------------------------
# cmd_bootstrap — register pre-runner migrations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bootstrap_registers_preexisting_versions(
    clean_pg: asyncpg.Connection,
) -> None:
    rc = await runner.cmd_bootstrap(clean_pg)
    assert rc == 0

    rows = await clean_pg.fetch(
        "SELECT version, name, down_sql FROM public.schema_migrations ORDER BY version"
    )
    expected = list(runner.PREEXISTING_VERSIONS.items())
    assert [(r["version"], r["name"]) for r in rows] == expected
    # Pre-existing migrations cannot be rolled back via this runner — they
    # were applied before it existed, so down_sql is NULL by design.
    assert all(r["down_sql"] is None for r in rows)


@pytest.mark.asyncio
async def test_bootstrap_is_idempotent(clean_pg: asyncpg.Connection) -> None:
    await runner.cmd_bootstrap(clean_pg)
    await runner.cmd_bootstrap(clean_pg)
    rows = await clean_pg.fetch("SELECT version FROM public.schema_migrations")
    assert len(rows) == len(runner.PREEXISTING_VERSIONS)


@pytest.mark.asyncio
async def test_bootstrap_then_up_skips_pre_existing(
    clean_pg: asyncpg.Connection, tmp_path: Path
) -> None:
    """After bootstrap, ``up`` should only apply migrations with versions
    higher than the registered pre-existing ones."""
    migdir = tmp_path / "migrations"
    migdir.mkdir()
    # File matching a pre-existing version — runner must not re-apply.
    (migdir / "001_mass_notifications_schema.sql").write_text(
        "CREATE TABLE public.should_not_exist (id INT);"
    )
    (migdir / "100_new.sql").write_text("CREATE TABLE public.new_table (id INT);")

    await runner.cmd_bootstrap(clean_pg)
    rc = await runner.cmd_up(clean_pg, migrations_dir=migdir)
    assert rc == 0

    # 001 was skipped (already applied per bootstrap), 100 applied.
    assert await clean_pg.fetchval("SELECT to_regclass('public.should_not_exist')") is None
    assert await clean_pg.fetchval("SELECT to_regclass('public.new_table')") is not None

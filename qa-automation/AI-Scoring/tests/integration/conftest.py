"""Postgres testcontainers fixtures for the integration suite.

Per SQLMigration.md §11.0, one fresh container per test session, with
each test running against an already-started Postgres+pgvector image. We
use ``pgvector/pgvector:pg16`` so the same fixture serves the runner
tests (no extensions needed) and the ``embeddings`` schema tests
(VECTOR columns).

Three fixtures are exposed:

- ``pg_dsn`` — session-scoped, the asyncpg-compatible connection string.

- ``clean_pg`` — per-test asyncpg connection that drops every schema the
  runner could create *before yielding*, so each test starts from a
  guaranteed-empty database. **Use for runner / migration tests** —
  those mutate DDL (CREATE TABLE inside a migration's own transaction)
  that SAVEPOINTs cannot un-do.

- ``pg_tx`` — per-test asyncpg connection inside a transaction that
  rolls back at test exit. Uses the session-scoped ``pg_migrated``
  fixture as the schema baseline. **Use for application-layer tests**
  that INSERT/UPDATE/DELETE against an already-migrated schema. Faster
  than ``clean_pg`` because the migrations aren't re-applied per test.

Golden-fixture helpers (loader at ``tests/fixtures/overall_formula/``)
live alongside the fixtures themselves; see ``load_overall_formula``.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
from testcontainers.postgres import PostgresContainer

# Make ``database.runner`` importable without an install step.
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Where canonical fixtures live (per SQLMigration.md §11.0).
_FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"
_FORMULA_FIXTURES_DIR = _FIXTURES_DIR / "overall_formula"

# Migrations applied at session start by ``pg_migrated``. Keep in
# numeric order — the SQL files DDL-depend on each other.
_WAVE_1_MIGRATIONS = (
    "004_create_schemas_and_teams.sql",
    "005_command_center_tables.sql",
    "006_qa_tables.sql",
    "007_embeddings_tables.sql",
    "008_indexes.sql",
)

_MIGRATIONS_DIR = _REPO_ROOT / "database" / "migrations"

# Acceptance threshold for compute_overall_score fixture tests
# (§3.6 + §11.3.2). Centralized so per-team fixture loaders all use
# the same value.
EPSILON: float = 0.05


@pytest.fixture(scope="session")
def pg_dsn():
    """Start a Postgres+pgvector container for the whole test session."""
    with PostgresContainer("pgvector/pgvector:pg16") as pg:
        raw = pg.get_connection_url()
        # testcontainers returns SQLAlchemy-style URLs; asyncpg wants the
        # bare ``postgresql://`` form.
        dsn = raw.replace("postgresql+psycopg2://", "postgresql://")
        yield dsn


@pytest_asyncio.fixture
async def clean_pg(pg_dsn):
    """Open an asyncpg connection against a guaranteed-clean DB.

    Drops everything the runner or any migration could have created on a
    previous test. Yields the live connection so the test can both invoke
    the runner (which opens its own connection via ``DATABASE_URL``) and
    introspect state directly.

    NOTE: when this fixture is used in the same session as ``pg_tx`` /
    ``pg_migrated``, the next test that depends on ``pg_migrated`` will
    re-apply the migrations because the schemas were just dropped. That
    self-healing is intentional — it keeps the two fixture families
    composable.
    """
    conn = await asyncpg.connect(pg_dsn)
    # Order matters only because of FKs; CASCADE removes dependents.
    await conn.execute("DROP TABLE IF EXISTS public.schema_migrations CASCADE")
    await conn.execute("DROP SCHEMA IF EXISTS qa CASCADE")
    await conn.execute("DROP SCHEMA IF EXISTS command_center CASCADE")
    await conn.execute("DROP SCHEMA IF EXISTS embeddings CASCADE")
    await conn.execute("DROP SCHEMA IF EXISTS mass_notifications CASCADE")
    # Drop any stray tables created by ad-hoc runner tests.
    rows = await conn.fetch(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
    )
    for r in rows:
        await conn.execute(f'DROP TABLE IF EXISTS public."{r["tablename"]}" CASCADE')
    try:
        yield conn
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# pg_migrated / pg_tx — application-layer test scaffolding (§11.0)
# ---------------------------------------------------------------------------


async def _apply_wave_1(dsn: str) -> None:
    """Apply 004 → 008 in order against the given DSN. Each migration
    runs in its own implicit transaction (single ``execute`` call)."""
    conn = await asyncpg.connect(dsn)
    try:
        # Self-heal: if a clean_pg test ran earlier in the session and
        # tore down our schemas, we re-apply. If pg_migrated already ran,
        # the migrations IF NOT EXISTS clauses (004) and table presence
        # checks make the re-apply a no-op for 004 only — but 005-008
        # would error on CREATE TABLE for existing tables. So we check
        # explicitly.
        already = await conn.fetchval(
            "SELECT to_regclass('qa.evaluations')"
        )
        if already is not None:
            return
        for name in _WAVE_1_MIGRATIONS:
            sql = (_MIGRATIONS_DIR / name).read_text(encoding="utf-8")
            await conn.execute(sql)
    finally:
        await conn.close()


@pytest_asyncio.fixture
async def pg_migrated(pg_dsn):
    """Returns a DSN against a database with all Wave-1 migrations
    applied. Self-heals if a ``clean_pg``-using test in the same session
    dropped the schemas first.

    The fixture is function-scoped (not session-scoped) so the self-heal
    can run on every test — but ``_apply_wave_1`` short-circuits when
    ``qa.evaluations`` already exists, so the cost is one round-trip
    per test in the steady state. Worth the simplicity over an
    invalidation dance with session-scoped state.
    """
    await _apply_wave_1(pg_dsn)
    return pg_dsn


@pytest_asyncio.fixture
async def pg_tx(pg_migrated):
    """Per-test asyncpg connection inside a transaction that rolls back
    on test exit.

    Use for application-layer tests that INSERT/UPDATE/DELETE against
    the already-migrated schema; do NOT use for tests that CREATE/DROP
    tables or schemas — those commits land outside the rollback envelope
    and bleed across tests. Use ``clean_pg`` for DDL-mutating tests.
    """
    conn = await asyncpg.connect(pg_migrated)
    tx = conn.transaction()
    await tx.start()
    try:
        yield conn
    finally:
        try:
            await tx.rollback()
        finally:
            await conn.close()


# ---------------------------------------------------------------------------
# Golden-fixture loader (§11.0 / §11.3.2)
# ---------------------------------------------------------------------------


def load_overall_formula(team: str) -> list[dict]:
    """Load golden Analyst_History rows + sheet-computed scores for the
    Phase A.5 compute_overall_score loop (§3.6 + §11.3.2).

    Returns the JSON file's ``rows`` list. Returns ``[]`` if no fixture
    file exists for the team yet — placeholder behavior so tests can be
    parametrized via ``pytest.mark.parametrize`` without exploding when
    fixtures haven't been authored.

    Per spec §3.8, each row is shape::

        {"evaluation_id": 12345,
         "sections": [{"section_id": "...", "numeric_score": 4, ...}, ...],
         "expected_score": 87.4}
    """
    fpath = _FORMULA_FIXTURES_DIR / f"{team}.json"
    if not fpath.exists():
        return []
    data = json.loads(fpath.read_text(encoding="utf-8"))
    return data.get("rows", [])

"""Postgres testcontainers fixtures for the integration suite.

Per SQLMigration.md §11.0, one fresh container per test session, with each
test running against an already-started Postgres+pgvector image. We use
``pgvector/pgvector:pg16`` so the same fixture serves the runner tests
(no extensions needed) and the future ``embeddings`` schema tests
(VECTOR columns).

Two fixtures are exposed:

- ``pg_dsn`` — session-scoped, the asyncpg-compatible connection string.
- ``clean_pg`` — per-test asyncpg connection that drops every schema the
  runner could create *before yielding*, so each test starts from a
  guaranteed-empty database. Use this for runner / migration tests.

Per-table tests added in later phases will introduce a third fixture
(``pg_tx``) that wraps each test in a SAVEPOINT-rolled-back transaction;
keeping that out of Phase 0 since runner tests mutate DDL (CREATE TABLE
inside a migration's own transaction) that SAVEPOINTs cannot un-do
without dropping the schemas.
"""

from __future__ import annotations

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

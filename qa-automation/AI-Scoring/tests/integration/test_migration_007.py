"""Tests for migration 007 — embeddings.* tables.

Per SQLMigration.md §11.5 floor:
  - ≥1 CHECK test per declared CHECK
  - ≥1 UPSERT-idempotency test per UNIQUE conflict target
  - Specific dim-class invariant test per §11.1
    (test_exactly_one_embedding_column_populated)

§7.6: 006 and 007 are independent — these tests apply 004 (for
public.teams FKs) but do NOT require 005 or 006.
"""

from __future__ import annotations

from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

from database import runner

REPO_ROOT = Path(__file__).resolve().parents[4]
MIGRATIONS_DIR = REPO_ROOT / "database" / "migrations"

UP_004 = (MIGRATIONS_DIR / "004_create_schemas_and_teams.sql").read_text()
UP_007 = (MIGRATIONS_DIR / "007_embeddings_tables.sql").read_text()
DOWN_007 = (MIGRATIONS_DIR / "007_embeddings_tables_down.sql").read_text()


@pytest_asyncio.fixture
async def pg_007(clean_pg: asyncpg.Connection) -> asyncpg.Connection:
    """clean_pg + 004 + 007. Skips 005/006 deliberately — independence
    of 006 and 007 (§7.6) means this works."""
    await clean_pg.execute(UP_004)
    await clean_pg.execute(UP_007)
    return clean_pg


# Helper: a vector literal of N dims for INSERT testing.
def _vec(n: int, fill: float = 0.1) -> str:
    return "[" + ",".join(f"{fill}" for _ in range(n)) + "]"


# ---------------------------------------------------------------------------
# embedding_models — CHECKs + UNIQUE (name, version) + current-per-modality
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_embedding_models_dimensions_capped_at_2000(
    pg_007: asyncpg.Connection,
) -> None:
    """pgvector's HNSW max dim — a model with dimensions > 2000 must be
    rejected here so we don't silently register one that can't be
    indexed."""
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await pg_007.execute(
            "INSERT INTO embeddings.embedding_models "
            "(name, version, dimensions, modality, is_cross_lingual, "
            " provider, is_open_source, cpu_deployable) "
            "VALUES ('big-model', 'v1', 3072, 'text', TRUE, 'google', FALSE, FALSE)"
        )


@pytest.mark.asyncio
async def test_embedding_models_dimensions_zero_rejected(
    pg_007: asyncpg.Connection,
) -> None:
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await pg_007.execute(
            "INSERT INTO embeddings.embedding_models "
            "(name, version, dimensions, modality, is_cross_lingual, "
            " provider, is_open_source, cpu_deployable) "
            "VALUES ('zero', 'v1', 0, 'text', TRUE, 'bge', TRUE, TRUE)"
        )


@pytest.mark.asyncio
async def test_embedding_models_modality_check(
    pg_007: asyncpg.Connection,
) -> None:
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await pg_007.execute(
            "INSERT INTO embeddings.embedding_models "
            "(name, version, dimensions, modality, is_cross_lingual, "
            " provider, is_open_source, cpu_deployable) "
            "VALUES ('odd-modality', 'v1', 1024, 'video', FALSE, 'bge', TRUE, TRUE)"
        )


@pytest.mark.asyncio
async def test_embedding_models_provider_check(
    pg_007: asyncpg.Connection,
) -> None:
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await pg_007.execute(
            "INSERT INTO embeddings.embedding_models "
            "(name, version, dimensions, modality, is_cross_lingual, "
            " provider, is_open_source, cpu_deployable) "
            "VALUES ('x', 'v1', 1024, 'text', FALSE, 'openai', FALSE, FALSE)"
        )


@pytest.mark.asyncio
async def test_embedding_models_is_current_requires_cpu_deployable(
    pg_007: asyncpg.Connection,
) -> None:
    """§5.3: cloud-only models cannot be `is_current` — the CPU-deployable
    gate keeps the LandGPT-deployment story honest."""
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await pg_007.execute(
            "INSERT INTO embeddings.embedding_models "
            "(name, version, dimensions, modality, is_cross_lingual, "
            " provider, is_open_source, cpu_deployable, is_current) "
            "VALUES ('cloud-only', 'v1', 1536, 'text', TRUE, 'google',"
            " FALSE, FALSE, TRUE)"
        )


@pytest.mark.asyncio
async def test_embedding_models_unique_name_version(
    pg_007: asyncpg.Connection,
) -> None:
    await pg_007.execute(
        "INSERT INTO embeddings.embedding_models "
        "(name, version, dimensions, modality, is_cross_lingual, "
        " provider, is_open_source, cpu_deployable) "
        "VALUES ('bge-m3', 'v1', 1024, 'text', TRUE, 'bge', TRUE, TRUE)"
    )
    with pytest.raises(asyncpg.exceptions.UniqueViolationError):
        await pg_007.execute(
            "INSERT INTO embeddings.embedding_models "
            "(name, version, dimensions, modality, is_cross_lingual, "
            " provider, is_open_source, cpu_deployable) "
            "VALUES ('bge-m3', 'v1', 1024, 'text', TRUE, 'bge', TRUE, TRUE)"
        )


@pytest.mark.asyncio
async def test_embedding_models_at_most_one_current_per_modality(
    pg_007: asyncpg.Connection,
) -> None:
    """Partial UNIQUE on is_current=TRUE — only one current model per
    modality. Multiple current models across DIFFERENT modalities OK."""
    await pg_007.execute(
        "INSERT INTO embeddings.embedding_models "
        "(name, version, dimensions, modality, is_cross_lingual, provider, "
        " is_open_source, cpu_deployable, is_current) "
        "VALUES ('bge-text', 'v1', 1024, 'text', TRUE, 'bge', TRUE, TRUE, TRUE), "
        "       ('multi-modal', 'v1', 1024, 'text+image', FALSE, 'qwen-team',"
        "        TRUE, TRUE, TRUE)"
    )
    with pytest.raises(asyncpg.exceptions.UniqueViolationError):
        await pg_007.execute(
            "INSERT INTO embeddings.embedding_models "
            "(name, version, dimensions, modality, is_cross_lingual, provider, "
            " is_open_source, cpu_deployable, is_current) "
            "VALUES ('bge-text-v2', 'v2', 1024, 'text', TRUE, 'bge', "
            "        TRUE, TRUE, TRUE)"
        )


# ---------------------------------------------------------------------------
# sop_documents — UNIQUE (team_id, version_tag), is_current partial UNIQUE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sop_documents_unique_team_version(
    pg_007: asyncpg.Connection,
) -> None:
    await pg_007.execute(
        "INSERT INTO embeddings.sop_documents "
        "(team_id, title, version_tag, language) "
        "VALUES ('sales', 'SOP A', 'v1', 'en')"
    )
    with pytest.raises(asyncpg.exceptions.UniqueViolationError):
        await pg_007.execute(
            "INSERT INTO embeddings.sop_documents "
            "(team_id, title, version_tag, language) "
            "VALUES ('sales', 'SOP A (alt)', 'v1', 'en')"
        )


@pytest.mark.asyncio
async def test_sop_documents_one_current_per_team_language(
    pg_007: asyncpg.Connection,
) -> None:
    """Partial UNIQUE on (team_id, language) WHERE is_current=TRUE —
    cross-language `is_current` rows for the same team coexist.

    Note version_tags differ per row: the full UNIQUE on
    (team_id, version_tag) means EN and ES can't share a tag even with
    different languages — that's a separate invariant; this test
    exercises only the language-partial UNIQUE.
    """
    await pg_007.execute(
        "INSERT INTO embeddings.sop_documents "
        "(team_id, title, version_tag, language, is_current) "
        "VALUES ('sales', 'EN', 'v1-en', 'en', TRUE), "
        "       ('sales', 'ES', 'v1-es', 'es', TRUE)"
    )
    with pytest.raises(asyncpg.exceptions.UniqueViolationError):
        await pg_007.execute(
            "INSERT INTO embeddings.sop_documents "
            "(team_id, title, version_tag, language, is_current) "
            "VALUES ('sales', 'EN-v2', 'v2-en', 'en', TRUE)"
        )


@pytest.mark.asyncio
async def test_sop_documents_team_id_fk(
    pg_007: asyncpg.Connection,
) -> None:
    with pytest.raises(asyncpg.exceptions.ForeignKeyViolationError):
        await pg_007.execute(
            "INSERT INTO embeddings.sop_documents "
            "(team_id, title, version_tag, language) "
            "VALUES ('phantom_team', 'SOP', 'v1', 'en')"
        )


# ---------------------------------------------------------------------------
# sop_chunks — UNIQUE (document_id, chunk_index), CASCADE on document delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sop_chunks_unique_doc_index(
    pg_007: asyncpg.Connection,
) -> None:
    did = await pg_007.fetchval(
        "INSERT INTO embeddings.sop_documents "
        "(team_id, title, version_tag, language) "
        "VALUES ('sales', 'SOP', 'v1', 'en') RETURNING id"
    )
    await pg_007.execute(
        "INSERT INTO embeddings.sop_chunks (document_id, chunk_index, text) "
        "VALUES ($1, 0, 'first chunk')",
        did,
    )
    with pytest.raises(asyncpg.exceptions.UniqueViolationError):
        await pg_007.execute(
            "INSERT INTO embeddings.sop_chunks (document_id, chunk_index, text) "
            "VALUES ($1, 0, 'duplicate')",
            did,
        )


@pytest.mark.asyncio
async def test_sop_chunks_cascade_on_document_delete(
    pg_007: asyncpg.Connection,
) -> None:
    did = await pg_007.fetchval(
        "INSERT INTO embeddings.sop_documents "
        "(team_id, title, version_tag, language) "
        "VALUES ('sales', 'SOP', 'v1', 'en') RETURNING id"
    )
    await pg_007.execute(
        "INSERT INTO embeddings.sop_chunks (document_id, chunk_index, text) "
        "VALUES ($1, 0, 'c0'), ($1, 1, 'c1')",
        did,
    )
    await pg_007.execute("DELETE FROM embeddings.sop_documents WHERE id = $1", did)
    assert (
        await pg_007.fetchval(
            "SELECT COUNT(*) FROM embeddings.sop_chunks WHERE document_id = $1",
            did,
        )
        == 0
    )


# ---------------------------------------------------------------------------
# sop_chunk_embeddings — exactly-one-dim CHECK + UNIQUE
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def chunk_id(pg_007: asyncpg.Connection) -> int:
    did = await pg_007.fetchval(
        "INSERT INTO embeddings.sop_documents "
        "(team_id, title, version_tag, language) "
        "VALUES ('sales', 'SOP', 'v1', 'en') RETURNING id"
    )
    return await pg_007.fetchval(
        "INSERT INTO embeddings.sop_chunks (document_id, chunk_index, text) "
        "VALUES ($1, 0, 'chunk text') RETURNING id",
        did,
    )


@pytest_asyncio.fixture
async def model_1024_id(pg_007: asyncpg.Connection) -> int:
    return await pg_007.fetchval(
        "INSERT INTO embeddings.embedding_models "
        "(name, version, dimensions, modality, is_cross_lingual, "
        " provider, is_open_source, cpu_deployable) "
        "VALUES ('bge-m3', 'v1', 1024, 'text', TRUE, 'bge', TRUE, TRUE) "
        "RETURNING id"
    )


@pytest.mark.asyncio
async def test_chunk_embeddings_exactly_one_dim_accepts_1024(
    pg_007: asyncpg.Connection, chunk_id: int, model_1024_id: int
) -> None:
    await pg_007.execute(
        "INSERT INTO embeddings.sop_chunk_embeddings "
        "(chunk_id, model_id, embedding_1024) "
        "VALUES ($1, $2, $3::vector)",
        chunk_id, model_1024_id, _vec(1024),
    )
    n = await pg_007.fetchval(
        "SELECT COUNT(*) FROM embeddings.sop_chunk_embeddings"
    )
    assert n == 1


@pytest.mark.asyncio
async def test_chunk_embeddings_exactly_one_dim_accepts_1536(
    pg_007: asyncpg.Connection, chunk_id: int
) -> None:
    mid = await pg_007.fetchval(
        "INSERT INTO embeddings.embedding_models "
        "(name, version, dimensions, modality, is_cross_lingual, "
        " provider, is_open_source, cpu_deployable) "
        "VALUES ('gemini-1536', 'v1', 1536, 'text', TRUE, 'google',"
        " FALSE, FALSE) RETURNING id"
    )
    await pg_007.execute(
        "INSERT INTO embeddings.sop_chunk_embeddings "
        "(chunk_id, model_id, embedding_1536) "
        "VALUES ($1, $2, $3::vector)",
        chunk_id, mid, _vec(1536),
    )
    n = await pg_007.fetchval(
        "SELECT COUNT(*) FROM embeddings.sop_chunk_embeddings"
    )
    assert n == 1


@pytest.mark.asyncio
async def test_chunk_embeddings_both_dims_populated_rejected(
    pg_007: asyncpg.Connection, chunk_id: int, model_1024_id: int
) -> None:
    """The exactly-one-dim CHECK means an indexer writing to both columns
    by mistake gets caught at write time — not silently stored as a row
    that KNN can't retrieve."""
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await pg_007.execute(
            "INSERT INTO embeddings.sop_chunk_embeddings "
            "(chunk_id, model_id, embedding_1024, embedding_1536) "
            "VALUES ($1, $2, $3::vector, $4::vector)",
            chunk_id, model_1024_id, _vec(1024), _vec(1536),
        )


@pytest.mark.asyncio
async def test_chunk_embeddings_neither_dim_populated_rejected(
    pg_007: asyncpg.Connection, chunk_id: int, model_1024_id: int
) -> None:
    """A row with no embedding is a corrupt-write signature."""
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await pg_007.execute(
            "INSERT INTO embeddings.sop_chunk_embeddings "
            "(chunk_id, model_id) VALUES ($1, $2)",
            chunk_id, model_1024_id,
        )


@pytest.mark.asyncio
async def test_chunk_embeddings_unique_chunk_model(
    pg_007: asyncpg.Connection, chunk_id: int, model_1024_id: int
) -> None:
    await pg_007.execute(
        "INSERT INTO embeddings.sop_chunk_embeddings "
        "(chunk_id, model_id, embedding_1024) "
        "VALUES ($1, $2, $3::vector)",
        chunk_id, model_1024_id, _vec(1024),
    )
    with pytest.raises(asyncpg.exceptions.UniqueViolationError):
        await pg_007.execute(
            "INSERT INTO embeddings.sop_chunk_embeddings "
            "(chunk_id, model_id, embedding_1024) "
            "VALUES ($1, $2, $3::vector)",
            chunk_id, model_1024_id, _vec(1024, fill=0.2),
        )


@pytest.mark.asyncio
async def test_chunk_embeddings_cascade_on_chunk_delete(
    pg_007: asyncpg.Connection, chunk_id: int, model_1024_id: int
) -> None:
    await pg_007.execute(
        "INSERT INTO embeddings.sop_chunk_embeddings "
        "(chunk_id, model_id, embedding_1024) "
        "VALUES ($1, $2, $3::vector)",
        chunk_id, model_1024_id, _vec(1024),
    )
    await pg_007.execute("DELETE FROM embeddings.sop_chunks WHERE id = $1", chunk_id)
    assert (
        await pg_007.fetchval(
            "SELECT COUNT(*) FROM embeddings.sop_chunk_embeddings "
            "WHERE chunk_id = $1",
            chunk_id,
        )
        == 0
    )


# ---------------------------------------------------------------------------
# embedding_runs — status CHECK
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_embedding_runs_status_check(
    pg_007: asyncpg.Connection, model_1024_id: int
) -> None:
    did = await pg_007.fetchval(
        "INSERT INTO embeddings.sop_documents "
        "(team_id, title, version_tag, language) "
        "VALUES ('sales', 'SOP', 'v1', 'en') RETURNING id"
    )
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await pg_007.execute(
            "INSERT INTO embeddings.embedding_runs "
            "(document_id, model_id, status) "
            "VALUES ($1, $2, 'unknown_status')",
            did, model_1024_id,
        )


# ---------------------------------------------------------------------------
# Down — drops every embeddings.* table
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_down_drops_all_embeddings_tables(
    pg_007: asyncpg.Connection,
) -> None:
    await pg_007.execute(DOWN_007)
    rows = await pg_007.fetch(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'embeddings'"
    )
    assert rows == []


# ---------------------------------------------------------------------------
# Runner integration — independence from 005/006 per §7.6
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runner_can_apply_007_without_005_or_006(
    clean_pg: asyncpg.Connection, tmp_path: Path
) -> None:
    """§7.6 says 006 and 007 are independent. Confirm the runner can
    apply 004 → 007 (skipping the 005/006 slot is not possible since
    file numbering must be contiguous, but 007 can be applied in
    isolation in a hypothetical embeddings-first deployment)."""
    import shutil
    migdir = tmp_path / "migrations"
    migdir.mkdir()
    shutil.copy(MIGRATIONS_DIR / "004_create_schemas_and_teams.sql", migdir)
    shutil.copy(MIGRATIONS_DIR / "004_create_schemas_and_teams_down.sql", migdir)
    # Renumber 007 → 005 in the temp dir so the runner sees a contiguous
    # 004 → 005 chain — this proves 007's DDL has no cross-migration
    # dependencies on 005's or 006's tables.
    shutil.copy(
        MIGRATIONS_DIR / "007_embeddings_tables.sql",
        migdir / "005_embeddings_tables.sql",
    )
    shutil.copy(
        MIGRATIONS_DIR / "007_embeddings_tables_down.sql",
        migdir / "005_embeddings_tables_down.sql",
    )

    rc = await runner.cmd_up(clean_pg, migrations_dir=migdir)
    assert rc == 0

    tables = await clean_pg.fetch(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'embeddings' "
        "ORDER BY tablename"
    )
    assert {r["tablename"] for r in tables} == {
        "embedding_models",
        "embedding_runs",
        "sop_chunk_embeddings",
        "sop_chunks",
        "sop_documents",
    }

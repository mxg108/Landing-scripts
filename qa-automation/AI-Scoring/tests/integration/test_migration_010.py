"""Tests for migration 010 — rubric versioning + public.teams ops config.

Per SQLMigration.md §11.5 floor:
  - ≥1 CHECK / UNIQUE test per declared constraint
  - State-machine test for the effective_until pair (active vs. retired
    rubric versions)
  - Cross-schema-FK test (qa.evaluations → qa.rubric_versions by string)
  - Seed verification: sales_v1 + member_support_v1 rows exist with the
    expected rubric_json shape; public.teams operational columns
    populated from the original JSON files.

Down test asserts the migration is reversible (the migration itself
ships without irreversible operations per §7.6 — every v1.3 migration
is reversible).
"""

from __future__ import annotations

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
DOWN_010 = (MIGRATIONS_DIR / "010_rubric_versioning_down.sql").read_text()


@pytest_asyncio.fixture
async def pg_010(clean_pg: asyncpg.Connection) -> asyncpg.Connection:
    """clean_pg + 004 + 005 + 006 + 009 + 010. Skips 007/008 — both
    are independent of the v1.3 surface."""
    await clean_pg.execute(UP_004)
    await clean_pg.execute(UP_005)
    await clean_pg.execute(UP_006)
    await clean_pg.execute(UP_009)
    await clean_pg.execute(UP_010)
    return clean_pg


_MODELS = '{"text": {"provider": "gemini", "model": "gemini-2.5-flash"}}'


# ---------------------------------------------------------------------------
# qa.rubric_versions — UNIQUE + FK + effective_until pair
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rubric_version_unique(pg_010: asyncpg.Connection) -> None:
    """Seed already inserted `sales_v1`; re-inserting must collide."""
    with pytest.raises(asyncpg.exceptions.UniqueViolationError):
        await pg_010.execute(
            "INSERT INTO qa.rubric_versions "
            "(rubric_version, team_id, rubric_json, effective_from) "
            "VALUES ('sales_v1', 'sales', '{}'::jsonb, NOW())"
        )


@pytest.mark.asyncio
async def test_rubric_version_team_id_fk_enforced(
    pg_010: asyncpg.Connection,
) -> None:
    with pytest.raises(asyncpg.exceptions.ForeignKeyViolationError):
        await pg_010.execute(
            "INSERT INTO qa.rubric_versions "
            "(rubric_version, team_id, rubric_json, effective_from) "
            "VALUES ('phantom_v1', 'phantom_team', '{}'::jsonb, NOW())"
        )


@pytest.mark.asyncio
async def test_rubric_effective_until_starts_null(
    pg_010: asyncpg.Connection,
) -> None:
    """Active rubric versions (seeded ones) have effective_until = NULL."""
    rows = await pg_010.fetch(
        "SELECT rubric_version, effective_until FROM qa.rubric_versions "
        "WHERE rubric_version IN ('sales_v1', 'member_support_v1')"
    )
    for r in rows:
        assert r["effective_until"] is None


@pytest.mark.asyncio
async def test_rubric_effective_until_can_retire_old_version(
    pg_010: asyncpg.Connection,
) -> None:
    """When a successor version goes live, the prior's effective_until
    gets set. The schema must allow this UPDATE."""
    await pg_010.execute(
        "INSERT INTO qa.rubric_versions "
        "(rubric_version, team_id, rubric_json, effective_from) "
        "VALUES ('sales_v2', 'sales', '{}'::jsonb, NOW())"
    )
    await pg_010.execute(
        "UPDATE qa.rubric_versions SET effective_until = NOW() "
        "WHERE rubric_version = 'sales_v1'"
    )
    sales_v1_retired = await pg_010.fetchval(
        "SELECT effective_until FROM qa.rubric_versions "
        "WHERE rubric_version = 'sales_v1'"
    )
    sales_v2_active = await pg_010.fetchval(
        "SELECT effective_until FROM qa.rubric_versions "
        "WHERE rubric_version = 'sales_v2'"
    )
    assert sales_v1_retired is not None
    assert sales_v2_active is None


# ---------------------------------------------------------------------------
# Seed verification — sales_v1 + member_support_v1
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_seed_includes_both_team_v1_rows(
    pg_010: asyncpg.Connection,
) -> None:
    rows = await pg_010.fetch(
        "SELECT rubric_version, team_id FROM qa.rubric_versions "
        "ORDER BY rubric_version"
    )
    assert [(r["rubric_version"], r["team_id"]) for r in rows] == [
        ("member_support_v1", "member_support"),
        ("sales_v1", "sales"),
    ]


@pytest.mark.asyncio
async def test_seed_rubric_json_contains_sections_and_scoring_prompt(
    pg_010: asyncpg.Connection,
) -> None:
    """Per §3.19.1, rubric_json carries `rubric_version`, `sections`,
    and `scoring_prompt`. Each section has an `id`."""
    sales = await pg_010.fetchval(
        "SELECT rubric_json FROM qa.rubric_versions "
        "WHERE rubric_version = 'sales_v1'"
    )
    sales_dict = json.loads(sales)
    assert sales_dict["rubric_version"] == "sales_v1"
    assert "sections" in sales_dict
    assert "scoring_prompt" in sales_dict
    assert len(sales_dict["sections"]) >= 10  # sales has 19 today
    assert all("id" in s for s in sales_dict["sections"])
    # Scoring prompt's referenced section IDs exist in sections — the
    # cross-validation §3.19.1 calls for at Pydantic load time.
    section_ids = {s["id"] for s in sales_dict["sections"]}
    for ref in sales_dict["scoring_prompt"].get("long_call_focus_sections", []):
        assert ref in section_ids, f"scoring_prompt references unknown section: {ref}"


@pytest.mark.asyncio
async def test_seed_member_support_rubric_json_shape(
    pg_010: asyncpg.Connection,
) -> None:
    ms = await pg_010.fetchval(
        "SELECT rubric_json FROM qa.rubric_versions "
        "WHERE rubric_version = 'member_support_v1'"
    )
    ms_dict = json.loads(ms)
    assert ms_dict["rubric_version"] == "member_support_v1"
    section_ids = {s["id"] for s in ms_dict["sections"]}
    # MS has its rubric anchored on greeting + identity_validation +
    # process_adherence + call_resolution — the sections the §3.14
    # human-review trigger keys off.
    assert "greeting" in section_ids
    assert "process_adherence" in section_ids
    assert "call_resolution" in section_ids


@pytest.mark.asyncio
async def test_seed_idx_team_effective_present(
    pg_010: asyncpg.Connection,
) -> None:
    """The lookup index for `which rubric was active for team X at time T`."""
    idx = await pg_010.fetchval(
        "SELECT indexname FROM pg_indexes "
        "WHERE schemaname = 'qa' AND indexname = 'idx_rubric_versions_team_effective'"
    )
    assert idx == "idx_rubric_versions_team_effective"


# ---------------------------------------------------------------------------
# qa.evaluations — rubric_version FK
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_evaluations_rubric_version_fk_accepts_seed(
    pg_010: asyncpg.Connection,
) -> None:
    """A new eval can stamp 'sales_v1' as its rubric_version (the FK
    points to the seed)."""
    eid = await pg_010.fetchval(
        "INSERT INTO qa.evaluations "
        "(team_id, agent_name_raw, state, source, rubric_version, models_used) "
        "VALUES ('sales', 'A', 'draft', 'ai', 'sales_v1', $1::jsonb) "
        "RETURNING id",
        _MODELS,
    )
    assert eid is not None


@pytest.mark.asyncio
async def test_evaluations_rubric_version_fk_rejects_unknown(
    pg_010: asyncpg.Connection,
) -> None:
    with pytest.raises(asyncpg.exceptions.ForeignKeyViolationError):
        await pg_010.execute(
            "INSERT INTO qa.evaluations "
            "(team_id, agent_name_raw, state, source, rubric_version, models_used) "
            "VALUES ('sales', 'A', 'draft', 'ai', 'phantom_rubric_v99', $1::jsonb)",
            _MODELS,
        )


@pytest.mark.asyncio
async def test_evaluations_rubric_version_nullable_for_backfill(
    pg_010: asyncpg.Connection,
) -> None:
    """Pre-v1.3 (backfilled historic) evals carry NULL rubric_version
    — they reference whatever rubric was in the JSON file at scoring time
    (no DB archive existed yet). The column MUST be nullable."""
    eid = await pg_010.fetchval(
        "INSERT INTO qa.evaluations "
        "(team_id, agent_name_raw, state, source, models_used) "
        "VALUES ('sales', 'A', 'draft', 'ai', $1::jsonb) RETURNING id",
        _MODELS,
    )
    assert eid is not None
    rv = await pg_010.fetchval(
        "SELECT rubric_version FROM qa.evaluations WHERE id = $1", eid
    )
    assert rv is None


# ---------------------------------------------------------------------------
# public.teams — operational columns (§6)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_teams_company_seeded(pg_010: asyncpg.Connection) -> None:
    rows = await pg_010.fetch("SELECT id, company FROM public.teams ORDER BY id")
    assert [(r["id"], r["company"]) for r in rows] == [
        ("member_support", "Landing Living LLC"),
        ("sales", "Landing Living LLC"),
    ]


@pytest.mark.asyncio
async def test_teams_stats_config_jsonb_seeded(
    pg_010: asyncpg.Connection,
) -> None:
    """Stats thresholds from the original JSON files landed in the
    stats_config JSONB column."""
    sales_stats = await pg_010.fetchval(
        "SELECT stats_config FROM public.teams WHERE id = 'sales'"
    )
    stats = json.loads(sales_stats)
    # These keys came from sales.json's `stats` block.
    assert "ewma_span" in stats
    assert "spc_sigma_multiplier" in stats
    assert "outlier_z_threshold" in stats


@pytest.mark.asyncio
async def test_teams_gemini_config_jsonb_seeded(
    pg_010: asyncpg.Connection,
) -> None:
    sales_gemini = await pg_010.fetchval(
        "SELECT gemini_config FROM public.teams WHERE id = 'sales'"
    )
    gemini = json.loads(sales_gemini)
    assert gemini["scoring_model"].startswith("gemini-")
    assert isinstance(gemini["scoring_temperature"], (int, float))


@pytest.mark.asyncio
async def test_teams_excluded_test_agents_array_seeded(
    pg_010: asyncpg.Connection,
) -> None:
    """`excluded_test_agents` is TEXT[]; seeded from the JSON list."""
    rows = await pg_010.fetch(
        "SELECT id, excluded_test_agents FROM public.teams ORDER BY id"
    )
    for r in rows:
        assert "Maximiliano Perez" in r["excluded_test_agents"]


@pytest.mark.asyncio
async def test_teams_sheets_config_present_and_legacy(
    pg_010: asyncpg.Connection,
) -> None:
    """sheets_config is JSONB (legacy — dropped at Phase D). Carries the
    score_destination column mapping for the Sheets-cutover transition."""
    sales_sheets = await pg_010.fetchval(
        "SELECT sheets_config FROM public.teams WHERE id = 'sales'"
    )
    sheets = json.loads(sales_sheets)
    assert "score_destination" in sheets
    assert "section_score_columns" in sheets["score_destination"]


@pytest.mark.asyncio
async def test_teams_updated_at_default_now(
    pg_010: asyncpg.Connection,
) -> None:
    """updated_at has a DEFAULT NOW() — the migration's ALTER + UPDATE
    populated it implicitly. New rows inserted without an explicit value
    also get NOW()."""
    rows = await pg_010.fetch(
        "SELECT id, updated_at FROM public.teams ORDER BY id"
    )
    for r in rows:
        assert r["updated_at"] is not None


# ---------------------------------------------------------------------------
# Down — drops the v1.3 artifacts cleanly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_down_drops_rubric_versions_table(
    pg_010: asyncpg.Connection,
) -> None:
    await pg_010.execute(DOWN_010)
    assert (
        await pg_010.fetchval("SELECT to_regclass('qa.rubric_versions')") is None
    )


@pytest.mark.asyncio
async def test_down_drops_rubric_version_column_on_evaluations(
    pg_010: asyncpg.Connection,
) -> None:
    await pg_010.execute(DOWN_010)
    col = await pg_010.fetch(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'qa' AND table_name = 'evaluations' "
        "  AND column_name = 'rubric_version'"
    )
    assert col == []


@pytest.mark.asyncio
async def test_down_drops_teams_operational_columns(
    pg_010: asyncpg.Connection,
) -> None:
    await pg_010.execute(DOWN_010)
    cols = await pg_010.fetch(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'teams' "
        "  AND column_name IN ('company', 'stats_config', 'gemini_config', "
        "                      'excluded_test_agents', 'sheets_config', 'updated_at')"
    )
    assert cols == []


# ---------------------------------------------------------------------------
# Runner integration — full chain 004 → 010
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runner_applies_004_005_006_009_010_in_sequence(
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
    ]:
        shutil.copy(MIGRATIONS_DIR / name, migdir)

    rc = await runner.cmd_up(clean_pg, migrations_dir=migdir)
    assert rc == 0
    applied = await clean_pg.fetch(
        "SELECT version FROM public.schema_migrations ORDER BY version"
    )
    assert [r["version"] for r in applied] == [4, 5, 6, 9, 10]

    # Sanity: both seed rubric versions are present after the runner
    # applied 010 (we didn't bypass the seed by re-running the SQL).
    n = await clean_pg.fetchval("SELECT COUNT(*) FROM qa.rubric_versions")
    assert n == 2

    # Down only 010 — qa.rubric_versions gone, public.teams loses ops
    # cols, but qa.evaluations table (from 006) stays intact.
    rc = await runner.cmd_down(clean_pg)
    assert rc == 0
    assert await clean_pg.fetchval("SELECT to_regclass('qa.rubric_versions')") is None
    assert await clean_pg.fetchval("SELECT to_regclass('qa.evaluations')") is not None
    # public.teams still has the 004 seed (member_support + sales rows).
    assert await clean_pg.fetchval("SELECT COUNT(*) FROM public.teams") == 2

"""Tests for migration 006 — qa.* tables.

Per SQLMigration.md §11.5 floor for every new table:
  - ≥1 CHECK test per declared CHECK
  - ≥1 UPSERT-idempotency test per UNIQUE conflict target

Plus per §11.5 for state-machine columns and cross-schema FKs:
  - qa.evaluations.state transitions (draft → approved → finalized)
  - qa.evaluations.command_center_call_id FK referential integrity
  - qa.evaluations.command_center_call_id NULL allowed (CC-outage path)

Pydantic JSONB validator tests are deliberately out of scope per
§7.5 (they ship in the "Pydantic models PR", a separate Wave-1 PR
that lives under qa-automation/AI-Scoring/backend/models/). The DB
columns are exercised here as raw JSONB.
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
DOWN_006 = (MIGRATIONS_DIR / "006_qa_tables_down.sql").read_text()


@pytest_asyncio.fixture
async def pg_006(clean_pg: asyncpg.Connection) -> asyncpg.Connection:
    """clean_pg + 004 + 005 + 006."""
    await clean_pg.execute(UP_004)
    await clean_pg.execute(UP_005)
    await clean_pg.execute(UP_006)
    return clean_pg


# Shared canonical models_used JSON for evaluations writes.
_MODELS = '{"text": {"provider": "gemini", "model": "gemini-2.5-flash"}}'


# ---------------------------------------------------------------------------
# qa.agents — UNIQUE (team_id, LOWER(name)) is case-insensitive
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agents_unique_team_lower_name_blocks_case_insensitive_dupe(
    pg_006: asyncpg.Connection,
) -> None:
    await pg_006.execute(
        "INSERT INTO qa.agents (team_id, name, email) VALUES ('sales', 'Alpha Rep', 'a@l.com')"
    )
    with pytest.raises(asyncpg.exceptions.UniqueViolationError):
        await pg_006.execute(
            "INSERT INTO qa.agents (team_id, name, email) "
            "VALUES ('sales', 'alpha rep', 'a@l.com')"
        )


@pytest.mark.asyncio
async def test_agents_same_name_different_team_ok(
    pg_006: asyncpg.Connection,
) -> None:
    await pg_006.execute(
        "INSERT INTO qa.agents (team_id, name, email) "
        "VALUES ('sales', 'Alpha Rep', 'a@l.com'), "
        "       ('member_support', 'Alpha Rep', 'a@l.com')"
    )
    assert await pg_006.fetchval("SELECT COUNT(*) FROM qa.agents") == 2


# ---------------------------------------------------------------------------
# qa.formula_versions — UNIQUE formula_version, FK target for evaluations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_formula_versions_formula_version_unique(
    pg_006: asyncpg.Connection,
) -> None:
    await pg_006.execute(
        "INSERT INTO qa.formula_versions "
        "(formula_version, team_id, formula_json, effective_from) "
        "VALUES ('sales_v1', 'sales', '{}'::jsonb, NOW())"
    )
    with pytest.raises(asyncpg.exceptions.UniqueViolationError):
        await pg_006.execute(
            "INSERT INTO qa.formula_versions "
            "(formula_version, team_id, formula_json, effective_from) "
            "VALUES ('sales_v1', 'sales', '{}'::jsonb, NOW())"
        )


# ---------------------------------------------------------------------------
# qa.evaluations — CHECK constraints (§3.4.3 relaxed pre-cutover form)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_evaluations_state_check_rejects_unknown(
    pg_006: asyncpg.Connection,
) -> None:
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await pg_006.execute(
            "INSERT INTO qa.evaluations (team_id, agent_name_raw, state, source, models_used) "
            "VALUES ('sales', 'A', 'unknown_state', 'ai', $1::jsonb)",
            _MODELS,
        )


@pytest.mark.asyncio
async def test_evaluations_source_check_rejects_unknown(
    pg_006: asyncpg.Connection,
) -> None:
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await pg_006.execute(
            "INSERT INTO qa.evaluations (team_id, agent_name_raw, state, source, models_used) "
            "VALUES ('sales', 'A', 'draft', 'unknown_source', $1::jsonb)",
            _MODELS,
        )


@pytest.mark.asyncio
async def test_evaluations_relaxed_check_accepts_approved_with_null_score(
    pg_006: asyncpg.Connection,
) -> None:
    """Pre-cutover: Stage 2 writes evaluator_email + approved_at but
    overall_score stays NULL until Stage 3 (ARRAYFORMULA readback).
    The §3.4.3 relaxed CHECK MUST accept this — the v0.4 strict CHECK
    would have rejected it."""
    eval_id = await pg_006.fetchval(
        """
        INSERT INTO qa.evaluations
            (team_id, agent_name_raw, state, source,
             evaluator_email, approved_at, models_used)
        VALUES ('sales', 'A', 'approved', 'ai', 'm@l.com', NOW(), $1::jsonb)
        RETURNING id
        """,
        _MODELS,
    )
    assert eval_id is not None


@pytest.mark.asyncio
async def test_evaluations_finalized_without_score_rejected(
    pg_006: asyncpg.Connection,
) -> None:
    """state='finalized' MUST have overall_score (both pre- and post-cutover)."""
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await pg_006.execute(
            """
            INSERT INTO qa.evaluations
                (team_id, agent_name_raw, state, source,
                 evaluator_email, approved_at, finalized_at, models_used)
            VALUES ('sales', 'A', 'finalized', 'ai',
                    'm@l.com', NOW(), NOW(), $1::jsonb)
            """,
            _MODELS,
        )


@pytest.mark.asyncio
async def test_evaluations_finalized_without_finalized_at_rejected(
    pg_006: asyncpg.Connection,
) -> None:
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await pg_006.execute(
            """
            INSERT INTO qa.evaluations
                (team_id, agent_name_raw, state, source,
                 evaluator_email, approved_at, overall_score, models_used)
            VALUES ('sales', 'A', 'finalized', 'ai',
                    'm@l.com', NOW(), 88.5, $1::jsonb)
            """,
            _MODELS,
        )


@pytest.mark.asyncio
async def test_evaluations_approved_without_evaluator_email_rejected(
    pg_006: asyncpg.Connection,
) -> None:
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await pg_006.execute(
            """
            INSERT INTO qa.evaluations
                (team_id, agent_name_raw, state, source, approved_at, models_used)
            VALUES ('sales', 'A', 'approved', 'ai', NOW(), $1::jsonb)
            """,
            _MODELS,
        )


@pytest.mark.asyncio
async def test_evaluations_scoring_status_check_rejects_unknown(
    pg_006: asyncpg.Connection,
) -> None:
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await pg_006.execute(
            "INSERT INTO qa.evaluations "
            "(team_id, agent_name_raw, state, source, scoring_status, models_used) "
            "VALUES ('sales', 'A', 'draft', 'ai', 'bogus_status', $1::jsonb)",
            _MODELS,
        )


@pytest.mark.asyncio
async def test_evaluations_ai_provider_primary_check_rejects_unknown(
    pg_006: asyncpg.Connection,
) -> None:
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await pg_006.execute(
            "INSERT INTO qa.evaluations "
            "(team_id, agent_name_raw, state, source, ai_provider_primary, models_used) "
            "VALUES ('sales', 'A', 'draft', 'ai', 'OpenAI', $1::jsonb)",
            _MODELS,
        )


# ---------------------------------------------------------------------------
# qa.evaluations — state-machine transitions per §11.5
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_state_transition_draft_to_approved(
    pg_006: asyncpg.Connection,
) -> None:
    eval_id = await pg_006.fetchval(
        "INSERT INTO qa.evaluations (team_id, agent_name_raw, state, source, models_used) "
        "VALUES ('sales', 'A', 'draft', 'ai', $1::jsonb) RETURNING id",
        _MODELS,
    )
    await pg_006.execute(
        "UPDATE qa.evaluations SET state='approved', evaluator_email='m@l.com', "
        "approved_at = NOW() WHERE id = $1",
        eval_id,
    )
    state = await pg_006.fetchval("SELECT state FROM qa.evaluations WHERE id = $1", eval_id)
    assert state == "approved"


@pytest.mark.asyncio
async def test_state_transition_approved_to_finalized(
    pg_006: asyncpg.Connection,
) -> None:
    eval_id = await pg_006.fetchval(
        "INSERT INTO qa.evaluations "
        "(team_id, agent_name_raw, state, source, evaluator_email, approved_at, models_used) "
        "VALUES ('sales', 'A', 'approved', 'ai', 'm@l.com', NOW(), $1::jsonb) "
        "RETURNING id",
        _MODELS,
    )
    await pg_006.execute(
        "UPDATE qa.evaluations SET state='finalized', overall_score=88.5, "
        "finalized_at = NOW() WHERE id = $1",
        eval_id,
    )
    state = await pg_006.fetchval("SELECT state FROM qa.evaluations WHERE id = $1", eval_id)
    assert state == "finalized"


# ---------------------------------------------------------------------------
# qa.evaluations — §3.4.1 dual partial UNIQUEs (dialpad_call_id + dialpad_link)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_partial_unique_call_id_allows_multiple_nulls(
    pg_006: asyncpg.Connection,
) -> None:
    """Backfilled rows may share NULL dialpad_call_id — the partial
    UNIQUE WHERE NOT NULL must allow that."""
    for _ in range(3):
        await pg_006.execute(
            "INSERT INTO qa.evaluations (team_id, agent_name_raw, state, source, models_used) "
            "VALUES ('sales', 'A', 'draft', 'ai', $1::jsonb)",
            _MODELS,
        )
    assert await pg_006.fetchval("SELECT COUNT(*) FROM qa.evaluations") == 3


@pytest.mark.asyncio
async def test_partial_unique_call_id_blocks_duplicate(
    pg_006: asyncpg.Connection,
) -> None:
    await pg_006.execute(
        "INSERT INTO qa.evaluations "
        "(team_id, agent_name_raw, state, source, dialpad_call_id, models_used) "
        "VALUES ('sales', 'A', 'draft', 'ai', 'c1', $1::jsonb)",
        _MODELS,
    )
    with pytest.raises(asyncpg.exceptions.UniqueViolationError):
        await pg_006.execute(
            "INSERT INTO qa.evaluations "
            "(team_id, agent_name_raw, state, source, dialpad_call_id, models_used) "
            "VALUES ('sales', 'A', 'draft', 'ai', 'c1', $1::jsonb)",
            _MODELS,
        )


@pytest.mark.asyncio
async def test_partial_unique_link_blocks_duplicate(
    pg_006: asyncpg.Connection,
) -> None:
    """The second partial UNIQUE on `dialpad_link` is the transition-period
    dedupe used by _find_row_by_dialpad_link (legacy Sheets path)."""
    link = "https://dialpad.com/callhistory/callreview/abc"
    await pg_006.execute(
        "INSERT INTO qa.evaluations "
        "(team_id, agent_name_raw, state, source, dialpad_link, models_used) "
        "VALUES ('sales', 'A', 'draft', 'ai', $1, $2::jsonb)",
        link, _MODELS,
    )
    with pytest.raises(asyncpg.exceptions.UniqueViolationError):
        await pg_006.execute(
            "INSERT INTO qa.evaluations "
            "(team_id, agent_name_raw, state, source, dialpad_link, models_used) "
            "VALUES ('sales', 'A', 'draft', 'ai', $1, $2::jsonb)",
            link, _MODELS,
        )


# ---------------------------------------------------------------------------
# qa.evaluations — cross-schema FK to command_center.calls per §11.5
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_command_center_call_id_fk_rejects_dangling_reference(
    pg_006: asyncpg.Connection,
) -> None:
    with pytest.raises(asyncpg.exceptions.ForeignKeyViolationError):
        await pg_006.execute(
            "INSERT INTO qa.evaluations "
            "(team_id, agent_name_raw, state, source, command_center_call_id, models_used) "
            "VALUES ('sales', 'A', 'draft', 'ai', 999999, $1::jsonb)",
            _MODELS,
        )


@pytest.mark.asyncio
async def test_command_center_call_id_nullable_for_cc_outage_path(
    pg_006: asyncpg.Connection,
) -> None:
    """§3.4: NULL on Phase B backfilled rows and on rows written during a
    CC outage — QA writes must NEVER be blocked by CC unavailability."""
    eid = await pg_006.fetchval(
        "INSERT INTO qa.evaluations "
        "(team_id, agent_name_raw, state, source, models_used) "
        "VALUES ('sales', 'A', 'draft', 'ai', $1::jsonb) RETURNING id",
        _MODELS,
    )
    assert eid is not None


@pytest.mark.asyncio
async def test_command_center_call_id_accepts_valid_calls_row(
    pg_006: asyncpg.Connection,
) -> None:
    """End-to-end: insert a calls row, then an eval that points at it."""
    call_id = await pg_006.fetchval(
        "INSERT INTO command_center.calls (team_id, dialpad_call_id, seen_via) "
        "VALUES ('sales', 'c1', 'webhook') RETURNING id"
    )
    eval_id = await pg_006.fetchval(
        "INSERT INTO qa.evaluations "
        "(team_id, agent_name_raw, state, source, command_center_call_id, models_used) "
        "VALUES ('sales', 'A', 'draft', 'ai', $1, $2::jsonb) RETURNING id",
        call_id, _MODELS,
    )
    assert eval_id is not None


@pytest.mark.asyncio
async def test_formula_version_fk_rejects_unknown_version(
    pg_006: asyncpg.Connection,
) -> None:
    with pytest.raises(asyncpg.exceptions.ForeignKeyViolationError):
        await pg_006.execute(
            "INSERT INTO qa.evaluations "
            "(team_id, agent_name_raw, state, source, formula_version, models_used) "
            "VALUES ('sales', 'A', 'draft', 'ai', 'never_existed', $1::jsonb)",
            _MODELS,
        )


# ---------------------------------------------------------------------------
# qa.evaluation_sections — CHECKs + UNIQUE + cascade
# ---------------------------------------------------------------------------


async def _make_eval(conn: asyncpg.Connection) -> int:
    return await conn.fetchval(
        "INSERT INTO qa.evaluations (team_id, agent_name_raw, state, source, models_used) "
        "VALUES ('sales', 'A', 'draft', 'ai', $1::jsonb) RETURNING id",
        _MODELS,
    )


@pytest.mark.asyncio
async def test_eval_sections_score_type_check_rejects_unknown(
    pg_006: asyncpg.Connection,
) -> None:
    eid = await _make_eval(pg_006)
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await pg_006.execute(
            "INSERT INTO qa.evaluation_sections "
            "(evaluation_id, section_id, section_number, score_type, "
            " numeric_score, score_source, ai_provider) "
            "VALUES ($1, 'greeting', 1, 'WAT', 4, 'ai', 'gemini')",
            eid,
        )


@pytest.mark.asyncio
async def test_eval_sections_score_source_check_rejects_unknown(
    pg_006: asyncpg.Connection,
) -> None:
    eid = await _make_eval(pg_006)
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await pg_006.execute(
            "INSERT INTO qa.evaluation_sections "
            "(evaluation_id, section_id, section_number, score_type, "
            " numeric_score, score_source) "
            "VALUES ($1, 'g', 1, 'manual_numeric', 4, 'WAT')",
            eid,
        )


@pytest.mark.asyncio
async def test_eval_sections_numeric_range_check(
    pg_006: asyncpg.Connection,
) -> None:
    eid = await _make_eval(pg_006)
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await pg_006.execute(
            "INSERT INTO qa.evaluation_sections "
            "(evaluation_id, section_id, section_number, score_type, "
            " numeric_score, score_source, ai_provider) "
            "VALUES ($1, 'g', 1, 'numeric', 6, 'ai', 'gemini')",
            eid,
        )


@pytest.mark.asyncio
async def test_eval_sections_binary_value_check(
    pg_006: asyncpg.Connection,
) -> None:
    eid = await _make_eval(pg_006)
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await pg_006.execute(
            "INSERT INTO qa.evaluation_sections "
            "(evaluation_id, section_id, section_number, score_type, "
            " binary_value, score_source, ai_provider) "
            "VALUES ($1, 'id', 2, 'binary', 'maybe', 'ai', 'gemini')",
            eid,
        )


@pytest.mark.asyncio
async def test_eval_sections_value_matches_type_numeric(
    pg_006: asyncpg.Connection,
) -> None:
    """score_type='numeric' requires numeric_score populated, binary NULL."""
    eid = await _make_eval(pg_006)
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        # binary_value populated for a numeric section
        await pg_006.execute(
            "INSERT INTO qa.evaluation_sections "
            "(evaluation_id, section_id, section_number, score_type, "
            " binary_value, score_source, ai_provider) "
            "VALUES ($1, 'g', 1, 'numeric', 'Y', 'ai', 'gemini')",
            eid,
        )


@pytest.mark.asyncio
async def test_eval_sections_value_matches_type_binary(
    pg_006: asyncpg.Connection,
) -> None:
    eid = await _make_eval(pg_006)
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        # numeric_score populated for a binary section
        await pg_006.execute(
            "INSERT INTO qa.evaluation_sections "
            "(evaluation_id, section_id, section_number, score_type, "
            " numeric_score, score_source, ai_provider) "
            "VALUES ($1, 'id', 2, 'binary', 3, 'ai', 'gemini')",
            eid,
        )


@pytest.mark.asyncio
async def test_eval_sections_value_matches_type_auto_value_numeric(
    pg_006: asyncpg.Connection,
) -> None:
    """`auto_value` accepts either numeric or binary (matching the
    underlying section's shape). A numeric auto_value row is valid."""
    eid = await _make_eval(pg_006)
    await pg_006.execute(
        "INSERT INTO qa.evaluation_sections "
        "(evaluation_id, section_id, section_number, score_type, "
        " numeric_score, score_source) "
        "VALUES ($1, 'auto_section', 9, 'auto_value', 5, 'auto_value')",
        eid,
    )


@pytest.mark.asyncio
async def test_eval_sections_value_matches_type_auto_value_binary(
    pg_006: asyncpg.Connection,
) -> None:
    eid = await _make_eval(pg_006)
    await pg_006.execute(
        "INSERT INTO qa.evaluation_sections "
        "(evaluation_id, section_id, section_number, score_type, "
        " binary_value, score_source) "
        "VALUES ($1, 'auto_doc', 9, 'auto_value', 'Y', 'auto_value')",
        eid,
    )


@pytest.mark.asyncio
async def test_eval_sections_ai_provider_required_when_ai(
    pg_006: asyncpg.Connection,
) -> None:
    eid = await _make_eval(pg_006)
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await pg_006.execute(
            "INSERT INTO qa.evaluation_sections "
            "(evaluation_id, section_id, section_number, score_type, "
            " numeric_score, score_source) "
            "VALUES ($1, 'g', 1, 'numeric', 4, 'ai')",
            eid,
        )


@pytest.mark.asyncio
async def test_eval_sections_ai_provider_null_for_manual(
    pg_006: asyncpg.Connection,
) -> None:
    eid = await _make_eval(pg_006)
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        # ai_provider populated but score_source != 'ai' — invalid
        await pg_006.execute(
            "INSERT INTO qa.evaluation_sections "
            "(evaluation_id, section_id, section_number, score_type, "
            " numeric_score, score_source, ai_provider) "
            "VALUES ($1, 'g', 1, 'manual_numeric', 4, 'manual', 'gemini')",
            eid,
        )


@pytest.mark.asyncio
async def test_eval_sections_ai_provider_value_rejects_unknown(
    pg_006: asyncpg.Connection,
) -> None:
    eid = await _make_eval(pg_006)
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await pg_006.execute(
            "INSERT INTO qa.evaluation_sections "
            "(evaluation_id, section_id, section_number, score_type, "
            " numeric_score, score_source, ai_provider) "
            "VALUES ($1, 'g', 1, 'numeric', 4, 'ai', 'OpenAI')",
            eid,
        )


@pytest.mark.asyncio
async def test_eval_sections_unique_evaluation_section(
    pg_006: asyncpg.Connection,
) -> None:
    eid = await _make_eval(pg_006)
    await pg_006.execute(
        "INSERT INTO qa.evaluation_sections "
        "(evaluation_id, section_id, section_number, score_type, "
        " numeric_score, score_source, ai_provider) "
        "VALUES ($1, 'g', 1, 'numeric', 4, 'ai', 'gemini')",
        eid,
    )
    with pytest.raises(asyncpg.exceptions.UniqueViolationError):
        await pg_006.execute(
            "INSERT INTO qa.evaluation_sections "
            "(evaluation_id, section_id, section_number, score_type, "
            " numeric_score, score_source, ai_provider) "
            "VALUES ($1, 'g', 1, 'numeric', 5, 'ai', 'gemini')",
            eid,
        )


@pytest.mark.asyncio
async def test_eval_sections_cascade_on_eval_delete(
    pg_006: asyncpg.Connection,
) -> None:
    """ON DELETE CASCADE on evaluation_id — admin-deleted evaluations must
    not leave orphan sections."""
    eid = await _make_eval(pg_006)
    await pg_006.execute(
        "INSERT INTO qa.evaluation_sections "
        "(evaluation_id, section_id, section_number, score_type, "
        " numeric_score, score_source, ai_provider) "
        "VALUES ($1, 'g', 1, 'numeric', 4, 'ai', 'gemini'), "
        "       ($1, 'r', 2, 'numeric', 4, 'ai', 'gemini')",
        eid,
    )
    await pg_006.execute("DELETE FROM qa.evaluations WHERE id = $1", eid)
    assert (
        await pg_006.fetchval(
            "SELECT COUNT(*) FROM qa.evaluation_sections WHERE evaluation_id = $1",
            eid,
        )
        == 0
    )


# ---------------------------------------------------------------------------
# qa.formula_compliance_sweeps — §3.13 new in v1.1
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pg_with_formula(pg_006: asyncpg.Connection) -> asyncpg.Connection:
    await pg_006.execute(
        "INSERT INTO qa.formula_versions "
        "(formula_version, team_id, formula_json, effective_from) "
        "VALUES ('sales_v1', 'sales', '{}'::jsonb, NOW())"
    )
    return pg_006


@pytest.mark.asyncio
async def test_sweep_delta_is_generated_signed_correctly(
    pg_with_formula: asyncpg.Connection,
) -> None:
    """`delta` is GENERATED ALWAYS AS (recomputed - original) STORED.
    Sign matters: positive delta means new formula > sheet; negative
    means new formula < sheet. The runbook (§7.7) uses both signs to
    spot pattern direction."""
    eid = await _make_eval(pg_with_formula)
    await pg_with_formula.execute(
        "INSERT INTO qa.formula_compliance_sweeps "
        "(evaluation_id, swept_formula_version, recomputed_score, "
        " original_score, flagged) "
        "VALUES ($1, 'sales_v1', 82.0, 78.0, TRUE)",
        eid,
    )
    delta = await pg_with_formula.fetchval(
        "SELECT delta FROM qa.formula_compliance_sweeps WHERE evaluation_id = $1",
        eid,
    )
    assert float(delta) == 4.0


@pytest.mark.asyncio
async def test_sweep_delta_negative_when_new_below_sheet(
    pg_with_formula: asyncpg.Connection,
) -> None:
    eid = await _make_eval(pg_with_formula)
    await pg_with_formula.execute(
        "INSERT INTO qa.formula_compliance_sweeps "
        "(evaluation_id, swept_formula_version, recomputed_score, "
        " original_score, flagged) "
        "VALUES ($1, 'sales_v1', 70.0, 78.0, TRUE)",
        eid,
    )
    delta = await pg_with_formula.fetchval(
        "SELECT delta FROM qa.formula_compliance_sweeps WHERE evaluation_id = $1",
        eid,
    )
    assert float(delta) == -8.0


@pytest.mark.asyncio
async def test_sweep_unique_eval_version_supports_on_conflict_do_nothing(
    pg_with_formula: asyncpg.Connection,
) -> None:
    """§3.13 write path uses ON CONFLICT (evaluation_id, swept_formula_version)
    DO NOTHING — re-running the sweep is idempotent."""
    eid = await _make_eval(pg_with_formula)
    for _ in range(2):
        await pg_with_formula.execute(
            "INSERT INTO qa.formula_compliance_sweeps "
            "(evaluation_id, swept_formula_version, recomputed_score, "
            " original_score, flagged) "
            "VALUES ($1, 'sales_v1', 82.0, 78.0, TRUE) "
            "ON CONFLICT (evaluation_id, swept_formula_version) DO NOTHING",
            eid,
        )
    assert (
        await pg_with_formula.fetchval(
            "SELECT COUNT(*) FROM qa.formula_compliance_sweeps WHERE evaluation_id = $1",
            eid,
        )
        == 1
    )


@pytest.mark.asyncio
async def test_sweep_multiple_versions_preserve_iteration_history(
    pg_with_formula: asyncpg.Connection,
) -> None:
    """Two formula versions sweeping the same eval produce two rows —
    that's what makes v2-vs-v1 flag-rate comparisons a single GROUP BY."""
    eid = await _make_eval(pg_with_formula)
    await pg_with_formula.execute(
        "INSERT INTO qa.formula_versions "
        "(formula_version, team_id, formula_json, effective_from) "
        "VALUES ('sales_v2', 'sales', '{}'::jsonb, NOW())"
    )
    await pg_with_formula.execute(
        "INSERT INTO qa.formula_compliance_sweeps "
        "(evaluation_id, swept_formula_version, recomputed_score, "
        " original_score, flagged) "
        "VALUES ($1, 'sales_v1', 82.0, 78.0, TRUE), "
        "       ($1, 'sales_v2', 79.0, 78.0, FALSE)",
        eid,
    )
    rows = await pg_with_formula.fetch(
        "SELECT swept_formula_version, flagged FROM qa.formula_compliance_sweeps "
        "WHERE evaluation_id = $1 ORDER BY swept_formula_version",
        eid,
    )
    assert [(r["swept_formula_version"], r["flagged"]) for r in rows] == [
        ("sales_v1", True),
        ("sales_v2", False),
    ]


@pytest.mark.asyncio
async def test_sweep_cascade_on_eval_delete(
    pg_with_formula: asyncpg.Connection,
) -> None:
    eid = await _make_eval(pg_with_formula)
    await pg_with_formula.execute(
        "INSERT INTO qa.formula_compliance_sweeps "
        "(evaluation_id, swept_formula_version, recomputed_score, "
        " original_score, flagged) "
        "VALUES ($1, 'sales_v1', 82.0, 78.0, TRUE)",
        eid,
    )
    await pg_with_formula.execute("DELETE FROM qa.evaluations WHERE id = $1", eid)
    assert (
        await pg_with_formula.fetchval(
            "SELECT COUNT(*) FROM qa.formula_compliance_sweeps WHERE evaluation_id = $1",
            eid,
        )
        == 0
    )


# ---------------------------------------------------------------------------
# qa.score_audit + qa.score_audit_archive — action CHECKs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_score_audit_action_check_accepts_evaluation_orphaned(
    pg_006: asyncpg.Connection,
) -> None:
    """v0.6 added the `evaluation_orphaned` action when an evaluation row
    is deleted (§3.9). Must be in the CHECK list."""
    await pg_006.execute(
        "INSERT INTO qa.score_audit (api_key_role, action, notes) "
        "VALUES ('privileged', 'evaluation_orphaned', 'manual delete')"
    )
    row = await pg_006.fetchval(
        "SELECT action FROM qa.score_audit ORDER BY id DESC LIMIT 1"
    )
    assert row == "evaluation_orphaned"


@pytest.mark.asyncio
async def test_score_audit_action_check_rejects_unknown(
    pg_006: asyncpg.Connection,
) -> None:
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await pg_006.execute(
            "INSERT INTO qa.score_audit (api_key_role, action) "
            "VALUES ('team', 'wat_is_this')"
        )


@pytest.mark.asyncio
async def test_score_audit_archive_accepts_same_action_set(
    pg_006: asyncpg.Connection,
) -> None:
    """The archive table mirrors score_audit's action vocabulary — cron-
    moved rows must satisfy the same CHECK."""
    await pg_006.execute(
        "INSERT INTO qa.score_audit_archive "
        "(original_id, timestamp, api_key_role, action) "
        "VALUES (1, NOW(), 'team', 'scored')"
    )
    cnt = await pg_006.fetchval("SELECT COUNT(*) FROM qa.score_audit_archive")
    assert cnt == 1


@pytest.mark.asyncio
async def test_score_audit_archive_action_check_rejects_unknown(
    pg_006: asyncpg.Connection,
) -> None:
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await pg_006.execute(
            "INSERT INTO qa.score_audit_archive "
            "(original_id, timestamp, api_key_role, action) "
            "VALUES (1, NOW(), 'team', 'wat_is_this')"
        )


# ---------------------------------------------------------------------------
# qa.agent_stat_points — UNIQUE evaluation_id, FK fan-out
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stat_points_unique_per_evaluation(
    pg_006: asyncpg.Connection,
) -> None:
    """Each evaluation finalize produces at most one stat point — the
    UNIQUE on evaluation_id prevents double-counting if a finalize is
    retried after an in-flight crash."""
    aid = await pg_006.fetchval(
        "INSERT INTO qa.agents (team_id, name, email) "
        "VALUES ('sales', 'A', 'a@l.com') RETURNING id"
    )
    eid = await _make_eval(pg_006)
    await pg_006.execute(
        "INSERT INTO qa.agent_stat_points "
        "(team_id, agent_id, evaluation_id, score, ewma, ewma_lambda) "
        "VALUES ('sales', $1, $2, 88.5, 88.2, 0.500)",
        aid, eid,
    )
    with pytest.raises(asyncpg.exceptions.UniqueViolationError):
        await pg_006.execute(
            "INSERT INTO qa.agent_stat_points "
            "(team_id, agent_id, evaluation_id, score, ewma, ewma_lambda) "
            "VALUES ('sales', $1, $2, 90.0, 89.0, 0.500)",
            aid, eid,
        )


# ---------------------------------------------------------------------------
# Down — drops every qa.* table cleanly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_down_drops_all_qa_tables(pg_006: asyncpg.Connection) -> None:
    await pg_006.execute(DOWN_006)
    rows = await pg_006.fetch(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'qa'"
    )
    assert rows == []


# ---------------------------------------------------------------------------
# Runner integration — 004 → 005 → 006 → down 006
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runner_applies_004_005_006_in_sequence(
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
    ]:
        shutil.copy(MIGRATIONS_DIR / name, migdir)

    rc = await runner.cmd_up(clean_pg, migrations_dir=migdir)
    assert rc == 0

    applied = await clean_pg.fetch(
        "SELECT version FROM public.schema_migrations ORDER BY version"
    )
    assert [r["version"] for r in applied] == [4, 5, 6]

    # Cross-check we can write an eval that FKs to a CC call — proves
    # the cross-schema FK was wired up correctly through the runner.
    cid = await clean_pg.fetchval(
        "INSERT INTO command_center.calls (team_id, dialpad_call_id, seen_via) "
        "VALUES ('sales', 'c1', 'webhook') RETURNING id"
    )
    eid = await clean_pg.fetchval(
        "INSERT INTO qa.evaluations "
        "(team_id, agent_name_raw, state, source, command_center_call_id, models_used) "
        "VALUES ('sales', 'A', 'draft', 'ai', $1, $2::jsonb) RETURNING id",
        cid, _MODELS,
    )
    assert eid is not None

    # Down 006 only — qa.* gone, command_center.* + public.teams stay.
    rc = await runner.cmd_down(clean_pg)
    assert rc == 0
    qa_tables = await clean_pg.fetch(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'qa'"
    )
    assert qa_tables == []
    # 005 + 004 untouched.
    assert (
        await clean_pg.fetchval("SELECT COUNT(*) FROM command_center.calls") == 1
    )
    assert await clean_pg.fetchval("SELECT COUNT(*) FROM public.teams") == 2

"""Tests for migration 009 — VP review additions (2026-06-24).

Per SQLMigration.md §11.5 floor:
  - ≥1 CHECK test per declared CHECK
  - ≥1 UPSERT-idempotency test per UNIQUE conflict target
  - State-machine transitions for `qa.coachings.status` and the
    `scoring_status` extension on `qa.evaluations`
  - CHECK pair-invariant tests (§3.4.3 v1.2 + §3.17 completion pair)
  - M:N cardinality tests (one coaching → many evals; one eval → many coachings)
  - EXPLAIN-plan for the new partial index

Plus a migration-mechanism test that asserts the down script restores
the original `scoring_status` CHECK constraint (§3.14 — failing loud
when a flagged_human_review row would block the rollback is the
correct behavior).
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
UP_005 = (MIGRATIONS_DIR / "005_command_center_tables.sql").read_text()
UP_006 = (MIGRATIONS_DIR / "006_qa_tables.sql").read_text()
UP_009 = (MIGRATIONS_DIR / "009_vp_review_additions.sql").read_text()
DOWN_009 = (MIGRATIONS_DIR / "009_vp_review_additions_down.sql").read_text()


@pytest_asyncio.fixture
async def pg_009(clean_pg: asyncpg.Connection) -> asyncpg.Connection:
    """clean_pg + 004 + 005 + 006 + 009. Skips 007/008 — independent."""
    await clean_pg.execute(UP_004)
    await clean_pg.execute(UP_005)
    await clean_pg.execute(UP_006)
    await clean_pg.execute(UP_009)
    return clean_pg


_MODELS = '{"text": {"provider": "gemini", "model": "gemini-2.5-flash"}}'


async def _make_eval(conn: asyncpg.Connection, dialpad_call_id: str | None = None) -> int:
    """Insert a draft evaluation; return its id. Optional unique id to
    avoid the (team_id, dialpad_call_id) partial UNIQUE colliding when
    a test needs more than one eval."""
    if dialpad_call_id is None:
        return await conn.fetchval(
            "INSERT INTO qa.evaluations "
            "(team_id, agent_name_raw, state, source, models_used) "
            "VALUES ('sales', 'A', 'draft', 'ai', $1::jsonb) RETURNING id",
            _MODELS,
        )
    return await conn.fetchval(
        "INSERT INTO qa.evaluations "
        "(team_id, agent_name_raw, state, source, dialpad_call_id, models_used) "
        "VALUES ('sales', 'A', 'draft', 'ai', $1, $2::jsonb) RETURNING id",
        dialpad_call_id, _MODELS,
    )


async def _make_agent(conn: asyncpg.Connection, name: str = "Alpha Rep") -> int:
    return await conn.fetchval(
        "INSERT INTO qa.agents (team_id, name, email) "
        "VALUES ('sales', $1, 'a@l.com') RETURNING id",
        name,
    )


# ---------------------------------------------------------------------------
# qa.evaluations — v1.2 column ALTERs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_needs_coaching_accepts_y_n_null(pg_009: asyncpg.Connection) -> None:
    """All three valid values pass."""
    for val in ("Y", "N", None):
        eid = await pg_009.fetchval(
            "INSERT INTO qa.evaluations "
            "(team_id, agent_name_raw, state, source, needs_coaching, models_used) "
            "VALUES ('sales', 'A', 'draft', 'ai', $1, $2::jsonb) RETURNING id",
            val, _MODELS,
        )
        assert eid is not None


@pytest.mark.asyncio
async def test_needs_coaching_rejects_other(pg_009: asyncpg.Connection) -> None:
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await pg_009.execute(
            "INSERT INTO qa.evaluations "
            "(team_id, agent_name_raw, state, source, needs_coaching, models_used) "
            "VALUES ('sales', 'A', 'draft', 'ai', 'maybe', $1::jsonb)",
            _MODELS,
        )


@pytest.mark.asyncio
async def test_action_plan_accepts_text_and_null(
    pg_009: asyncpg.Connection,
) -> None:
    eid = await pg_009.fetchval(
        "INSERT INTO qa.evaluations "
        "(team_id, agent_name_raw, state, source, action_plan, models_used) "
        "VALUES ('sales', 'A', 'draft', 'ai', $1, $2::jsonb) RETURNING id",
        "Practice greeting cadence; review SOP step 3.", _MODELS,
    )
    assert eid is not None
    val = await pg_009.fetchval(
        "SELECT action_plan FROM qa.evaluations WHERE id = $1", eid
    )
    assert "SOP step 3" in val


# ---------------------------------------------------------------------------
# qa.evaluations — human_review pair invariant (§3.4.3 v1.2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_human_review_pair_blocks_completed_without_required(
    pg_009: asyncpg.Connection,
) -> None:
    """Setting `human_review_completed_at` without `human_review_required_at`
    is nonsensical (you can't complete a review that was never started)."""
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await pg_009.execute(
            "INSERT INTO qa.evaluations "
            "(team_id, agent_name_raw, state, source, "
            " human_review_completed_at, models_used) "
            "VALUES ('sales', 'A', 'draft', 'ai', NOW(), $1::jsonb)",
            _MODELS,
        )


@pytest.mark.asyncio
async def test_human_review_pair_allows_required_alone(
    pg_009: asyncpg.Connection,
) -> None:
    """A flagged eval awaiting review has required_at but no completed_at."""
    eid = await pg_009.fetchval(
        "INSERT INTO qa.evaluations "
        "(team_id, agent_name_raw, state, source, scoring_status, "
        " human_review_required_at, models_used) "
        "VALUES ('sales', 'A', 'draft', 'ai', 'flagged_human_review', "
        " NOW(), $1::jsonb) RETURNING id",
        _MODELS,
    )
    assert eid is not None


@pytest.mark.asyncio
async def test_human_review_pair_allows_both_set(
    pg_009: asyncpg.Connection,
) -> None:
    """After a human reviewer finishes, both timestamps are populated."""
    eid = await pg_009.fetchval(
        "INSERT INTO qa.evaluations "
        "(team_id, agent_name_raw, state, source, scoring_status, "
        " human_review_required_at, human_review_completed_at, models_used) "
        "VALUES ('sales', 'A', 'draft', 'ai', 'complete', "
        " NOW() - INTERVAL '10 minutes', NOW(), $1::jsonb) RETURNING id",
        _MODELS,
    )
    assert eid is not None


@pytest.mark.asyncio
async def test_human_review_pair_allows_both_null(
    pg_009: asyncpg.Connection,
) -> None:
    """The trigger never fired — both timestamps stay NULL."""
    eid = await _make_eval(pg_009)
    pair = await pg_009.fetchrow(
        "SELECT human_review_required_at, human_review_completed_at "
        "FROM qa.evaluations WHERE id = $1", eid
    )
    assert pair["human_review_required_at"] is None
    assert pair["human_review_completed_at"] is None


# ---------------------------------------------------------------------------
# qa.evaluations — extended scoring_status CHECK
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scoring_status_accepts_flagged_human_review(
    pg_009: asyncpg.Connection,
) -> None:
    eid = await pg_009.fetchval(
        "INSERT INTO qa.evaluations "
        "(team_id, agent_name_raw, state, source, scoring_status, "
        " human_review_required_at, models_used) "
        "VALUES ('sales', 'A', 'draft', 'ai', 'flagged_human_review', "
        " NOW(), $1::jsonb) RETURNING id",
        _MODELS,
    )
    assert eid is not None


@pytest.mark.asyncio
async def test_scoring_status_accepts_all_pre_v1_2_values(
    pg_009: asyncpg.Connection,
) -> None:
    """Re-asserting the extended CHECK didn't accidentally drop any
    pre-existing value."""
    for val in ('complete', 'flagged_long_call', 'errored',
                'landgpt_unavailable_routed_to_gemini'):
        eid = await pg_009.fetchval(
            "INSERT INTO qa.evaluations "
            "(team_id, agent_name_raw, state, source, scoring_status, models_used) "
            "VALUES ('sales', 'A', 'draft', 'ai', $1, $2::jsonb) RETURNING id",
            val, _MODELS,
        )
        assert eid is not None


@pytest.mark.asyncio
async def test_scoring_status_check_still_rejects_unknown(
    pg_009: asyncpg.Connection,
) -> None:
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await pg_009.execute(
            "INSERT INTO qa.evaluations "
            "(team_id, agent_name_raw, state, source, scoring_status, models_used) "
            "VALUES ('sales', 'A', 'draft', 'ai', 'wat_is_this', $1::jsonb)",
            _MODELS,
        )


# ---------------------------------------------------------------------------
# qa.tags — UNIQUE slug + seed + active default
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tags_slug_unique(pg_009: asyncpg.Connection) -> None:
    """Seed already inserted `sop`; re-inserting must collide."""
    with pytest.raises(asyncpg.exceptions.UniqueViolationError):
        await pg_009.execute(
            "INSERT INTO qa.tags (slug, category, label) "
            "VALUES ('sop', 'human_review_focus', 'SOP Duplicate')"
        )


@pytest.mark.asyncio
async def test_tags_seed_has_four_human_review_focus_tags(
    pg_009: asyncpg.Connection,
) -> None:
    rows = await pg_009.fetch(
        "SELECT slug, label FROM qa.tags "
        "WHERE category = 'human_review_focus' ORDER BY slug"
    )
    slugs = [(r["slug"], r["label"]) for r in rows]
    assert slugs == [
        ("efficiency", "Efficiency"),
        ("hard_skills", "Hard Skills"),
        ("soft_skills", "Soft Skills"),
        ("sop", "SOP"),
    ]


@pytest.mark.asyncio
async def test_tags_active_defaults_true(pg_009: asyncpg.Connection) -> None:
    """The seed didn't specify `active`; the column default kicks in."""
    rows = await pg_009.fetch(
        "SELECT active FROM qa.tags WHERE category = 'human_review_focus'"
    )
    assert all(r["active"] is True for r in rows)


@pytest.mark.asyncio
async def test_tags_seed_idempotent_on_conflict(
    pg_009: asyncpg.Connection,
) -> None:
    """ON CONFLICT (slug) DO NOTHING — re-applying the seed line doesn't
    duplicate or error."""
    await pg_009.execute(
        "INSERT INTO qa.tags (slug, category, label) VALUES "
        "    ('sop',          'human_review_focus', 'SOP-redup'), "
        "    ('soft_skills',  'human_review_focus', 'Soft-redup'), "
        "    ('hard_skills',  'human_review_focus', 'Hard-redup'), "
        "    ('efficiency',   'human_review_focus', 'Eff-redup') "
        "ON CONFLICT (slug) DO NOTHING"
    )
    # Still exactly the 4 original seed rows.
    n = await pg_009.fetchval(
        "SELECT COUNT(*) FROM qa.tags WHERE category = 'human_review_focus'"
    )
    assert n == 4
    # Original label preserved (DO NOTHING didn't overwrite).
    sop_label = await pg_009.fetchval(
        "SELECT label FROM qa.tags WHERE slug = 'sop'"
    )
    assert sop_label == "SOP"


# ---------------------------------------------------------------------------
# qa.evaluation_tags — CHECKs + UNIQUE + provenance coexistence
# ---------------------------------------------------------------------------


async def _tag_id(conn: asyncpg.Connection, slug: str) -> int:
    return await conn.fetchval(
        "SELECT id FROM qa.tags WHERE slug = $1", slug
    )


@pytest.mark.asyncio
async def test_eval_tags_source_check_rejects_unknown(
    pg_009: asyncpg.Connection,
) -> None:
    eid = await _make_eval(pg_009)
    tid = await _tag_id(pg_009, "sop")
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await pg_009.execute(
            "INSERT INTO qa.evaluation_tags "
            "(evaluation_id, tag_id, source) VALUES ($1, $2, 'bogus_source')",
            eid, tid,
        )


@pytest.mark.asyncio
async def test_eval_tags_source_check_accepts_all_three(
    pg_009: asyncpg.Connection,
) -> None:
    eid = await _make_eval(pg_009)
    tid = await _tag_id(pg_009, "sop")
    for source in ("manager", "ai", "auto"):
        await pg_009.execute(
            "INSERT INTO qa.evaluation_tags "
            "(evaluation_id, tag_id, source) VALUES ($1, $2, $3)",
            eid, tid, source,
        )
    assert (
        await pg_009.fetchval(
            "SELECT COUNT(*) FROM qa.evaluation_tags WHERE evaluation_id = $1",
            eid,
        )
        == 3
    )


@pytest.mark.asyncio
async def test_eval_tags_unique_per_eval_tag_source(
    pg_009: asyncpg.Connection,
) -> None:
    """Same (eval, tag, source) triple → conflict. Different source → OK."""
    eid = await _make_eval(pg_009)
    tid = await _tag_id(pg_009, "sop")
    await pg_009.execute(
        "INSERT INTO qa.evaluation_tags (evaluation_id, tag_id, source, created_by) "
        "VALUES ($1, $2, 'manager', 'm@l.com')",
        eid, tid,
    )
    with pytest.raises(asyncpg.exceptions.UniqueViolationError):
        await pg_009.execute(
            "INSERT INTO qa.evaluation_tags (evaluation_id, tag_id, source) "
            "VALUES ($1, $2, 'manager')",
            eid, tid,
        )


@pytest.mark.asyncio
async def test_eval_tags_same_tag_different_sources_coexist(
    pg_009: asyncpg.Connection,
) -> None:
    """Manager + AI both tag the same eval with the same tag — both rows
    persist (agreement is information). Forward-compatible with §8.6."""
    eid = await _make_eval(pg_009)
    tid = await _tag_id(pg_009, "soft_skills")
    await pg_009.execute(
        "INSERT INTO qa.evaluation_tags (evaluation_id, tag_id, source, created_by) "
        "VALUES ($1, $2, 'manager', 'm@l.com'), "
        "       ($1, $2, 'ai',      NULL)",
        eid, tid,
    )
    rows = await pg_009.fetch(
        "SELECT source FROM qa.evaluation_tags WHERE evaluation_id = $1 "
        "ORDER BY source",
        eid,
    )
    assert [r["source"] for r in rows] == ["ai", "manager"]


@pytest.mark.asyncio
async def test_eval_tags_cascade_on_eval_delete(
    pg_009: asyncpg.Connection,
) -> None:
    eid = await _make_eval(pg_009)
    tid = await _tag_id(pg_009, "sop")
    await pg_009.execute(
        "INSERT INTO qa.evaluation_tags (evaluation_id, tag_id, source) "
        "VALUES ($1, $2, 'manager')",
        eid, tid,
    )
    await pg_009.execute("DELETE FROM qa.evaluations WHERE id = $1", eid)
    assert (
        await pg_009.fetchval(
            "SELECT COUNT(*) FROM qa.evaluation_tags WHERE evaluation_id = $1",
            eid,
        )
        == 0
    )


@pytest.mark.asyncio
async def test_eval_tags_fk_blocks_unknown_tag(
    pg_009: asyncpg.Connection,
) -> None:
    eid = await _make_eval(pg_009)
    with pytest.raises(asyncpg.exceptions.ForeignKeyViolationError):
        await pg_009.execute(
            "INSERT INTO qa.evaluation_tags (evaluation_id, tag_id, source) "
            "VALUES ($1, 999999, 'manager')",
            eid,
        )


# ---------------------------------------------------------------------------
# qa.coachings — CHECKs + completion pair invariant
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_coachings_conducted_by_role_rejects_unknown(
    pg_009: asyncpg.Connection,
) -> None:
    aid = await _make_agent(pg_009)
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await pg_009.execute(
            "INSERT INTO qa.coachings (agent_id, team_id, conducted_by_role) "
            "VALUES ($1, 'sales', 'ceo')",
            aid,
        )


@pytest.mark.asyncio
async def test_coachings_conducted_by_role_accepts_all_four(
    pg_009: asyncpg.Connection,
) -> None:
    aid = await _make_agent(pg_009)
    for role in ("team_lead", "manager", "hr", "external"):
        cid = await pg_009.fetchval(
            "INSERT INTO qa.coachings (agent_id, team_id, conducted_by_role) "
            "VALUES ($1, 'sales', $2) RETURNING id",
            aid, role,
        )
        assert cid is not None


@pytest.mark.asyncio
async def test_coachings_status_check(pg_009: asyncpg.Connection) -> None:
    aid = await _make_agent(pg_009)
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await pg_009.execute(
            "INSERT INTO qa.coachings "
            "(agent_id, team_id, conducted_by_role, status) "
            "VALUES ($1, 'sales', 'manager', 'archived')",
            aid,
        )


@pytest.mark.asyncio
async def test_coachings_agent_attitude_rejects_unknown(
    pg_009: asyncpg.Connection,
) -> None:
    aid = await _make_agent(pg_009)
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await pg_009.execute(
            "INSERT INTO qa.coachings "
            "(agent_id, team_id, conducted_by_role, agent_attitude) "
            "VALUES ($1, 'sales', 'manager', 'angry')",
            aid,
        )


@pytest.mark.asyncio
async def test_coachings_agent_attitude_accepts_all_six(
    pg_009: asyncpg.Connection,
) -> None:
    aid = await _make_agent(pg_009)
    for attitude in ("receptive", "engaged", "neutral",
                     "defensive", "dismissive", "mixed"):
        cid = await pg_009.fetchval(
            "INSERT INTO qa.coachings "
            "(agent_id, team_id, conducted_by_role, agent_attitude) "
            "VALUES ($1, 'sales', 'manager', $2) RETURNING id",
            aid, attitude,
        )
        assert cid is not None


@pytest.mark.asyncio
async def test_coachings_attitude_allows_null(
    pg_009: asyncpg.Connection,
) -> None:
    """Pending coachings don't have an attitude yet."""
    aid = await _make_agent(pg_009)
    cid = await pg_009.fetchval(
        "INSERT INTO qa.coachings (agent_id, team_id, conducted_by_role) "
        "VALUES ($1, 'sales', 'manager') RETURNING id",
        aid,
    )
    attitude = await pg_009.fetchval(
        "SELECT agent_attitude FROM qa.coachings WHERE id = $1", cid
    )
    assert attitude is None


@pytest.mark.asyncio
async def test_coachings_completion_pair_blocks_completed_without_summary(
    pg_009: asyncpg.Connection,
) -> None:
    aid = await _make_agent(pg_009)
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await pg_009.execute(
            "INSERT INTO qa.coachings "
            "(agent_id, team_id, conducted_by_role, status, "
            " completed_at, completed_by) "
            "VALUES ($1, 'sales', 'manager', 'completed', NOW(), 'm@l.com')",
            aid,
        )


@pytest.mark.asyncio
async def test_coachings_completion_pair_blocks_completed_without_completed_by(
    pg_009: asyncpg.Connection,
) -> None:
    aid = await _make_agent(pg_009)
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await pg_009.execute(
            "INSERT INTO qa.coachings "
            "(agent_id, team_id, conducted_by_role, status, "
            " coaching_summary, completed_at) "
            "VALUES ($1, 'sales', 'manager', 'completed', "
            " 'discussed SOP step 3', NOW())",
            aid,
        )


@pytest.mark.asyncio
async def test_coachings_completed_with_all_three_succeeds(
    pg_009: asyncpg.Connection,
) -> None:
    aid = await _make_agent(pg_009)
    cid = await pg_009.fetchval(
        "INSERT INTO qa.coachings "
        "(agent_id, team_id, conducted_by_role, status, "
        " coaching_summary, completed_at, completed_by, agent_attitude) "
        "VALUES ($1, 'sales', 'manager', 'completed', "
        " 'discussed SOP step 3; agent receptive', NOW(), 'm@l.com', 'receptive') "
        "RETURNING id",
        aid,
    )
    assert cid is not None


@pytest.mark.asyncio
async def test_coachings_pending_allows_null_summary(
    pg_009: asyncpg.Connection,
) -> None:
    """A pending coaching is created BEFORE the session; summary is NULL."""
    aid = await _make_agent(pg_009)
    cid = await pg_009.fetchval(
        "INSERT INTO qa.coachings "
        "(agent_id, team_id, conducted_by_role, action_plan, "
        " action_plan_deadline) "
        "VALUES ($1, 'sales', 'team_lead', "
        " 'Listen to 3 model calls; demo SOP step 3', "
        " NOW() + INTERVAL '7 days') RETURNING id",
        aid,
    )
    assert cid is not None
    row = await pg_009.fetchrow(
        "SELECT status, coaching_summary, completed_at "
        "FROM qa.coachings WHERE id = $1", cid,
    )
    assert row["status"] == "pending"
    assert row["coaching_summary"] is None
    assert row["completed_at"] is None


@pytest.mark.asyncio
async def test_coachings_team_id_fk_enforced(
    pg_009: asyncpg.Connection,
) -> None:
    aid = await _make_agent(pg_009)
    with pytest.raises(asyncpg.exceptions.ForeignKeyViolationError):
        await pg_009.execute(
            "INSERT INTO qa.coachings (agent_id, team_id, conducted_by_role) "
            "VALUES ($1, 'phantom_team', 'manager')",
            aid,
        )


@pytest.mark.asyncio
async def test_coachings_agent_id_fk_enforced(
    pg_009: asyncpg.Connection,
) -> None:
    with pytest.raises(asyncpg.exceptions.ForeignKeyViolationError):
        await pg_009.execute(
            "INSERT INTO qa.coachings (agent_id, team_id, conducted_by_role) "
            "VALUES (999999, 'sales', 'manager')",
        )


# ---------------------------------------------------------------------------
# qa.coachings — status state-machine transitions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_coaching_pending_to_completed_transition(
    pg_009: asyncpg.Connection,
) -> None:
    aid = await _make_agent(pg_009)
    cid = await pg_009.fetchval(
        "INSERT INTO qa.coachings (agent_id, team_id, conducted_by_role) "
        "VALUES ($1, 'sales', 'manager') RETURNING id",
        aid,
    )
    await pg_009.execute(
        "UPDATE qa.coachings SET status='completed', "
        " coaching_summary='went well', completed_at=NOW(), "
        " completed_by='m@l.com' WHERE id = $1",
        cid,
    )
    status = await pg_009.fetchval(
        "SELECT status FROM qa.coachings WHERE id = $1", cid
    )
    assert status == "completed"


@pytest.mark.asyncio
async def test_coaching_pending_to_cancelled_transition(
    pg_009: asyncpg.Connection,
) -> None:
    """Cancellation doesn't require summary/completed_at — completion pair
    CHECK only fires for status='completed'."""
    aid = await _make_agent(pg_009)
    cid = await pg_009.fetchval(
        "INSERT INTO qa.coachings (agent_id, team_id, conducted_by_role) "
        "VALUES ($1, 'sales', 'team_lead') RETURNING id",
        aid,
    )
    await pg_009.execute(
        "UPDATE qa.coachings SET status='cancelled' WHERE id = $1", cid,
    )
    status = await pg_009.fetchval(
        "SELECT status FROM qa.coachings WHERE id = $1", cid
    )
    assert status == "cancelled"


# ---------------------------------------------------------------------------
# qa.coaching_evaluations — M:N semantics + cascade boundaries
# ---------------------------------------------------------------------------


async def _make_coaching(
    conn: asyncpg.Connection, agent_id: int, role: str = "manager"
) -> int:
    return await conn.fetchval(
        "INSERT INTO qa.coachings (agent_id, team_id, conducted_by_role) "
        "VALUES ($1, 'sales', $2) RETURNING id",
        agent_id, role,
    )


@pytest.mark.asyncio
async def test_coaching_evals_unique_pair(
    pg_009: asyncpg.Connection,
) -> None:
    aid = await _make_agent(pg_009)
    cid = await _make_coaching(pg_009, aid)
    eid = await _make_eval(pg_009)
    await pg_009.execute(
        "INSERT INTO qa.coaching_evaluations (coaching_id, evaluation_id) "
        "VALUES ($1, $2)",
        cid, eid,
    )
    with pytest.raises(asyncpg.exceptions.UniqueViolationError):
        await pg_009.execute(
            "INSERT INTO qa.coaching_evaluations (coaching_id, evaluation_id) "
            "VALUES ($1, $2)",
            cid, eid,
        )


@pytest.mark.asyncio
async def test_coaching_evals_cascade_on_coaching_delete(
    pg_009: asyncpg.Connection,
) -> None:
    aid = await _make_agent(pg_009)
    cid = await _make_coaching(pg_009, aid)
    eid = await _make_eval(pg_009)
    await pg_009.execute(
        "INSERT INTO qa.coaching_evaluations (coaching_id, evaluation_id) "
        "VALUES ($1, $2)",
        cid, eid,
    )
    await pg_009.execute("DELETE FROM qa.coachings WHERE id = $1", cid)
    assert (
        await pg_009.fetchval(
            "SELECT COUNT(*) FROM qa.coaching_evaluations "
            "WHERE coaching_id = $1", cid,
        )
        == 0
    )
    # The evaluation itself is untouched.
    assert (
        await pg_009.fetchval(
            "SELECT COUNT(*) FROM qa.evaluations WHERE id = $1", eid
        )
        == 1
    )


@pytest.mark.asyncio
async def test_coaching_evals_fk_blocks_evaluation_delete(
    pg_009: asyncpg.Connection,
) -> None:
    """No CASCADE on evaluation_id — deleting an eval that's still linked
    to a coaching must fail loudly (admin path explicitly handles)."""
    aid = await _make_agent(pg_009)
    cid = await _make_coaching(pg_009, aid)
    eid = await _make_eval(pg_009)
    await pg_009.execute(
        "INSERT INTO qa.coaching_evaluations (coaching_id, evaluation_id) "
        "VALUES ($1, $2)",
        cid, eid,
    )
    with pytest.raises(asyncpg.exceptions.ForeignKeyViolationError):
        await pg_009.execute("DELETE FROM qa.evaluations WHERE id = $1", eid)


@pytest.mark.asyncio
async def test_one_coaching_covers_multiple_evals(
    pg_009: asyncpg.Connection,
) -> None:
    """Manager reviews 3 calls in one sit-down — one coaching, three
    join rows."""
    aid = await _make_agent(pg_009)
    cid = await _make_coaching(pg_009, aid)
    eids = [await _make_eval(pg_009, f"call-{i}") for i in range(3)]
    for eid in eids:
        await pg_009.execute(
            "INSERT INTO qa.coaching_evaluations (coaching_id, evaluation_id) "
            "VALUES ($1, $2)",
            cid, eid,
        )
    assert (
        await pg_009.fetchval(
            "SELECT COUNT(*) FROM qa.coaching_evaluations WHERE coaching_id = $1",
            cid,
        )
        == 3
    )


@pytest.mark.asyncio
async def test_one_eval_appears_in_multiple_coachings(
    pg_009: asyncpg.Connection,
) -> None:
    """Escalation: TL → Manager → HR all coach on the same call. Three
    coaching rows, three join rows on the same eval."""
    aid = await _make_agent(pg_009)
    cid_tl = await _make_coaching(pg_009, aid, "team_lead")
    cid_mgr = await _make_coaching(pg_009, aid, "manager")
    cid_hr = await _make_coaching(pg_009, aid, "hr")
    eid = await _make_eval(pg_009)
    for cid in (cid_tl, cid_mgr, cid_hr):
        await pg_009.execute(
            "INSERT INTO qa.coaching_evaluations (coaching_id, evaluation_id) "
            "VALUES ($1, $2)",
            cid, eid,
        )
    rows = await pg_009.fetch(
        "SELECT c.conducted_by_role FROM qa.coaching_evaluations ce "
        "JOIN qa.coachings c ON c.id = ce.coaching_id "
        "WHERE ce.evaluation_id = $1 ORDER BY c.conducted_by_role",
        eid,
    )
    assert [r["conducted_by_role"] for r in rows] == ["hr", "manager", "team_lead"]


# ---------------------------------------------------------------------------
# EXPLAIN-plan — partial human-review queue index
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idx_eval_human_review_queue_used_by_queue_read(
    pg_009: asyncpg.Connection,
) -> None:
    """The queue read: "evals waiting for human review, by team, oldest
    first" — should use the partial index."""
    await pg_009.execute("SET enable_seqscan = OFF")
    rows = await pg_009.fetch(
        "EXPLAIN SELECT id, human_review_required_at FROM qa.evaluations "
        "WHERE state = 'draft' AND scoring_status = 'flagged_human_review' "
        "AND team_id = 'sales' ORDER BY human_review_required_at"
    )
    plan = "\n".join(r["QUERY PLAN"] for r in rows)
    assert "idx_eval_human_review_queue" in plan


# ---------------------------------------------------------------------------
# Down — drops every v1.2 artifact
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_down_drops_all_v1_2_tables(
    pg_009: asyncpg.Connection,
) -> None:
    await pg_009.execute(DOWN_009)
    for table in ("qa.tags", "qa.evaluation_tags",
                  "qa.coachings", "qa.coaching_evaluations"):
        assert await pg_009.fetchval(f"SELECT to_regclass('{table}')") is None


@pytest.mark.asyncio
async def test_down_removes_v1_2_columns_on_evaluations(
    pg_009: asyncpg.Connection,
) -> None:
    await pg_009.execute(DOWN_009)
    cols = await pg_009.fetch(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'qa' AND table_name = 'evaluations' "
        "AND column_name IN ('needs_coaching', 'action_plan', "
        " 'human_review_required_at', 'human_review_completed_at')"
    )
    assert cols == []


@pytest.mark.asyncio
async def test_down_restores_original_scoring_status_check(
    pg_009: asyncpg.Connection,
) -> None:
    """After down, the original (pre-v1.2) scoring_status CHECK is back —
    so 'flagged_human_review' is no longer accepted."""
    await pg_009.execute(DOWN_009)
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await pg_009.execute(
            "INSERT INTO qa.evaluations "
            "(team_id, agent_name_raw, state, source, scoring_status, models_used) "
            "VALUES ('sales', 'A', 'draft', 'ai', 'flagged_human_review', "
            " $1::jsonb)",
            _MODELS,
        )


@pytest.mark.asyncio
async def test_down_fails_when_flagged_human_review_rows_exist(
    pg_009: asyncpg.Connection,
) -> None:
    """If any row still carries the new scoring_status value when down
    runs, the ADD CONSTRAINT validation fails. By design — the operator
    must clear or migrate those rows manually before rolling back."""
    await pg_009.execute(
        "INSERT INTO qa.evaluations "
        "(team_id, agent_name_raw, state, source, scoring_status, "
        " human_review_required_at, models_used) "
        "VALUES ('sales', 'A', 'draft', 'ai', 'flagged_human_review', "
        " NOW(), $1::jsonb)",
        _MODELS,
    )
    with pytest.raises(asyncpg.PostgresError):
        await pg_009.execute(DOWN_009)


# ---------------------------------------------------------------------------
# Runner integration — full chain 004 → 009
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runner_applies_004_005_006_009_in_sequence(
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
    ]:
        shutil.copy(MIGRATIONS_DIR / name, migdir)

    rc = await runner.cmd_up(clean_pg, migrations_dir=migdir)
    assert rc == 0
    applied = await clean_pg.fetch(
        "SELECT version FROM public.schema_migrations ORDER BY version"
    )
    assert [r["version"] for r in applied] == [4, 5, 6, 9]

    # Smoke: the seed tags exist + new evaluations column is queryable.
    n_tags = await clean_pg.fetchval(
        "SELECT COUNT(*) FROM qa.tags WHERE category = 'human_review_focus'"
    )
    assert n_tags == 4
    needs_coaching = await clean_pg.fetchval(
        "SELECT needs_coaching FROM qa.evaluations LIMIT 1"
    )
    assert needs_coaching is None  # no rows yet — column accessible

    # Down only 009 — qa.tags/coachings gone, qa.evaluations stays.
    rc = await runner.cmd_down(clean_pg)
    assert rc == 0
    assert await clean_pg.fetchval("SELECT to_regclass('qa.tags')") is None
    assert await clean_pg.fetchval("SELECT to_regclass('qa.evaluations')") is not None

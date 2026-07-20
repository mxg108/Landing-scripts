"""fetch_call_context against a real Postgres — the §5 triple-key match
(entry_point → call_id → master, in that order) + the holds pull."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

from backend.services import eval_store
from backend.services.cc_context import fetch_call_context

REPO_ROOT = Path(__file__).resolve().parents[4]
MIGRATIONS_DIR = REPO_ROOT / "database" / "migrations"

T0 = datetime(2026, 7, 15, 10, 0, 30, tzinfo=timezone.utc)


def _at(seconds: int) -> datetime:
    return datetime.fromtimestamp(T0.timestamp() + seconds, tz=timezone.utc)


@pytest_asyncio.fixture
async def pg_ctx(clean_pg: asyncpg.Connection, pg_dsn: str, monkeypatch):
    """004/005/006/016/017 + the eval_store pool (cc_context reads
    through it) pointed at the container DB."""
    for name in (
        "004_create_schemas_and_teams.sql",
        "005_command_center_tables.sql",
        "006_qa_tables.sql",
        "016_cc_dispositions_ai_csat_holds.sql",
        "017_calls_seen_via_stats_pull.sql",
    ):
        await clean_pg.execute((MIGRATIONS_DIR / name).read_text(encoding="utf-8"))
    monkeypatch.setenv("DATABASE_URL", pg_dsn)
    monkeypatch.setattr(eval_store, "_pool", None)
    monkeypatch.setattr(eval_store, "_pool_lock", None)
    try:
        yield clean_pg
    finally:
        await eval_store.close_pool()


async def _seed_call(conn: asyncpg.Connection, **cols) -> int:
    base = dict(
        team_id="member_support",
        dialpad_call_id="DP-LEG",
        dialpad_entry_point_call_id="DP-ENTRY",
        dialpad_master_call_id="DP-MASTER",
        seen_via="webhook",
        connected_at=T0,
        total_hold_seconds=160,
        disposition_category="Access & Entry",
        disposition="Smart-lock failure",
        disposition_source="webhook",
        ai_csat=4.5,
    )
    base.update(cols)
    names = ", ".join(base)
    placeholders = ", ".join(f"${i + 1}" for i in range(len(base)))
    return await conn.fetchval(
        f"INSERT INTO command_center.calls ({names}) "
        f"VALUES ({placeholders}) RETURNING id",
        *base.values(),
    )


@pytest.mark.asyncio
async def test_match_prefers_entry_point(pg_ctx: asyncpg.Connection) -> None:
    await _seed_call(pg_ctx)
    ctx = await fetch_call_context(
        "member_support",
        entry_point_call_id="DP-ENTRY",
        dialpad_call_id="DP-LEG",
        master_call_id="DP-MASTER",
    )
    assert ctx is not None
    assert ctx.matched_by == "entry_point"
    assert ctx.disposition_category == "Access & Entry"
    assert ctx.disposition == "Smart-lock failure"
    assert ctx.ai_csat == 4.5
    assert ctx.total_hold_seconds == 160


@pytest.mark.asyncio
async def test_match_falls_back_call_id_then_master(
    pg_ctx: asyncpg.Connection,
) -> None:
    await _seed_call(pg_ctx)
    by_leg = await fetch_call_context(
        "member_support",
        entry_point_call_id="NOT-A-MATCH",
        dialpad_call_id="DP-LEG",
        master_call_id="DP-MASTER",
    )
    assert by_leg is not None and by_leg.matched_by == "call_id"
    by_master = await fetch_call_context(
        "member_support",
        entry_point_call_id="NOT-A-MATCH",
        dialpad_call_id="ALSO-NOT",
        master_call_id="DP-MASTER",
    )
    assert by_master is not None and by_master.matched_by == "master"


@pytest.mark.asyncio
async def test_no_match_returns_none(pg_ctx: asyncpg.Connection) -> None:
    """Webhook era predates the call → ungrounded scoring, not an error."""
    await _seed_call(pg_ctx)
    ctx = await fetch_call_context(
        "member_support",
        entry_point_call_id="X", dialpad_call_id="Y", master_call_id="Z",
    )
    assert ctx is None


@pytest.mark.asyncio
async def test_team_scoping(pg_ctx: asyncpg.Connection) -> None:
    """A sales-team call never grounds a member_support eval."""
    await _seed_call(pg_ctx, team_id="sales")
    ctx = await fetch_call_context(
        "member_support", dialpad_call_id="DP-LEG",
    )
    assert ctx is None


@pytest.mark.asyncio
async def test_holds_pulled_in_order(pg_ctx: asyncpg.Connection) -> None:
    cc_id = await _seed_call(pg_ctx)
    for start, end, secs, ended_by in (
        (662, 720, 58, "hangup"),
        (195, 297, 102, "connected"),
    ):
        await pg_ctx.execute(
            """
            INSERT INTO command_center.hold_intervals
                (team_id, dialpad_call_id, call_id, started_at, ended_at,
                 seconds, ended_by)
            VALUES ('member_support', 'DP-LEG', $1, $2, $3, $4, $5)
            """,
            cc_id, _at(start), _at(end), secs, ended_by,
        )
    ctx = await fetch_call_context("member_support", dialpad_call_id="DP-LEG")
    assert ctx is not None
    assert [(h.seconds,) for h in ctx.holds] == [(102,), (58,)]
    assert ctx.holds[0].started_at == _at(195)


@pytest.mark.asyncio
async def test_webhook_row_has_hold_truth(pg_ctx: asyncpg.Connection) -> None:
    await _seed_call(pg_ctx)
    ctx = await fetch_call_context("member_support", dialpad_call_id="DP-LEG")
    assert ctx is not None
    assert ctx.has_hold_truth is True


@pytest.mark.asyncio
async def test_stats_pull_row_lacks_hold_truth(
    pg_ctx: asyncpg.Connection,
) -> None:
    """A stats_pull-created row (migration 017) grounds dispositions but
    NOT holds — total_hold_seconds=0 there is a schema default, and the
    block must not render it as a verified no-holds claim."""
    await _seed_call(
        pg_ctx, seen_via="stats_pull",
        disposition_source="stats_pull", total_hold_seconds=0,
    )
    ctx = await fetch_call_context("member_support", dialpad_call_id="DP-LEG")
    assert ctx is not None
    assert ctx.has_hold_truth is False
    assert ctx.disposition_category == "Access & Entry"

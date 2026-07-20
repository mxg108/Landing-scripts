"""Stats-pull fill semantics against a real Postgres (interim CC v1
ingestion): rows are CREATED for calls the webhook era never saw
(seen_via='stats_pull', migration 017), existing webhook stamps win the
seam, evals fill by the triple key, and re-pulls are idempotent — the
30-min is_today loop's dedupe is structural, not diff-based."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

from backend.services import eval_store
from backend.services.disposition_pull import StatsRecord, fill_records

REPO_ROOT = Path(__file__).resolve().parents[4]
MIGRATIONS_DIR = REPO_ROOT / "database" / "migrations"

_MODELS = '{"text": {"provider": "gemini"}}'


@pytest_asyncio.fixture
async def pg_fill(clean_pg: asyncpg.Connection, pg_dsn: str, monkeypatch):
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

    # One call already webhook-stamped: its disposition must survive.
    await clean_pg.execute(
        """
        INSERT INTO command_center.calls
            (team_id, dialpad_call_id, seen_via,
             disposition_category, disposition, disposition_source)
        VALUES ('member_support', 'CALL-WEBHOOK', 'webhook',
                'Billing', 'Refund', 'webhook')
        """
    )
    # Evals: one already stamped at Stage 1; one post-fix (entry-point id
    # in its own column); one HISTORICAL (per-leg id only, entry NULL —
    # the pre-fix Stage-1 writer) whose dialpad_link embeds the
    # entry-point id, the only join key those rows have.
    await clean_pg.execute(
        f"""
        INSERT INTO qa.evaluations
            (team_id, agent_name_raw, state, source, models_used,
             dialpad_call_id, dialpad_entry_point_call_id, dialpad_link,
             dialpad_disposition_category, dialpad_disposition)
        VALUES
            ('member_support', 'A', 'draft', 'ai', '{_MODELS}'::jsonb,
             'CALL-WEBHOOK', NULL, NULL, 'Billing', 'Refund'),
            ('member_support', 'B', 'draft', 'ai', '{_MODELS}'::jsonb,
             'LEG-OLD', 'CALL-NEW', NULL, NULL, NULL),
            ('member_support', 'C', 'draft', 'ai', '{_MODELS}'::jsonb,
             'LEG-HISTORIC', NULL,
             'https://dialpad.com/callhistory/callreview/CALL-NEW',
             NULL, NULL)
        """
    )
    try:
        yield clean_pg
    finally:
        await eval_store.close_pool()


_T0 = datetime(2026, 7, 20, 10, 0, 30, tzinfo=timezone.utc)

_RECORDS = [
    # Export tries to overwrite the webhook stamp — must lose the seam.
    StatsRecord("CALL-WEBHOOK", "Access & Entry", "Lockout"),
    # Never seen: row must be CREATED with disposition + metadata.
    StatsRecord(
        "CALL-NEW", "Access & Entry", "Smart-lock failure",
        direction="inbound", external_number="+15550100",
        agent_name="Jane Agent", connected_at=_T0,
    ),
    # Mid-day undispositioned call: row created, disposition NULL.
    StatsRecord("CALL-PENDING", None, None),
]


@pytest.mark.asyncio
async def test_fill_creates_rows_and_respects_webhook_seam(
    pg_fill: asyncpg.Connection,
) -> None:
    report = await fill_records("member_support", _RECORDS)
    assert report["rows_in_export"] == 3
    assert report["with_disposition"] == 2
    assert report["calls_written"] == 2       # CALL-NEW + CALL-PENDING
    # eval B via its entry-point column + eval C via its dialpad_link
    # (historic rows carry the entry-point id ONLY there).
    assert report["evals_filled"] == 2
    assert report["row_failures"] == 0

    # Webhook stamp untouched.
    webhook_row = await pg_fill.fetchrow(
        "SELECT disposition_category, disposition_source, seen_via "
        "FROM command_center.calls WHERE dialpad_call_id = 'CALL-WEBHOOK'"
    )
    assert webhook_row["disposition_category"] == "Billing"
    assert webhook_row["disposition_source"] == "webhook"
    assert webhook_row["seen_via"] == "webhook"

    # New call CREATED with disposition + export metadata.
    new_row = await pg_fill.fetchrow(
        "SELECT * FROM command_center.calls WHERE dialpad_call_id = 'CALL-NEW'"
    )
    assert new_row["seen_via"] == "stats_pull"
    assert new_row["disposition_category"] == "Access & Entry"
    assert new_row["disposition"] == "Smart-lock failure"
    assert new_row["disposition_source"] == "stats_pull"
    assert new_row["direction"] == "inbound"
    assert new_row["agent_name"] == "Jane Agent"
    assert new_row["connected_at"] == _T0
    assert new_row["total_hold_seconds"] == 0  # schema default — NOT truth

    # Undispositioned call seeded with NULLs (pair CHECK satisfied).
    pending = await pg_fill.fetchrow(
        "SELECT disposition_category, disposition_source, seen_via "
        "FROM command_center.calls WHERE dialpad_call_id = 'CALL-PENDING'"
    )
    assert pending["seen_via"] == "stats_pull"
    assert pending["disposition_category"] is None
    assert pending["disposition_source"] is None

    # Eval B joined via its entry-point column, eval C via its
    # dialpad_link (historic shape); stamped eval A untouched.
    for name in ("B", "C"):
        filled = await pg_fill.fetchrow(
            "SELECT dialpad_disposition_category, dialpad_disposition "
            "FROM qa.evaluations WHERE agent_name_raw = $1", name,
        )
        assert filled["dialpad_disposition_category"] == "Access & Entry"
        assert filled["dialpad_disposition"] == "Smart-lock failure"
    stamped = await pg_fill.fetchval(
        "SELECT dialpad_disposition_category FROM qa.evaluations "
        "WHERE agent_name_raw = 'A'"
    )
    assert stamped == "Billing"


@pytest.mark.asyncio
async def test_late_disposition_fills_seeded_row(
    pg_fill: asyncpg.Connection,
) -> None:
    """The loop's core case: pull N sees the call mid-day without a
    disposition; pull N+1 carries it — the seeded row updates in place."""
    await fill_records("member_support", [StatsRecord("CALL-LATE", None, None)])
    await fill_records(
        "member_support",
        [StatsRecord("CALL-LATE", "Reservation & Stay Changes", None)],
    )
    row = await pg_fill.fetchrow(
        "SELECT disposition_category, disposition, disposition_source "
        "FROM command_center.calls WHERE dialpad_call_id = 'CALL-LATE'"
    )
    assert row["disposition_category"] == "Reservation & Stay Changes"
    assert row["disposition"] is None
    assert row["disposition_source"] == "stats_pull"


@pytest.mark.asyncio
async def test_repull_is_idempotent(pg_fill: asyncpg.Connection) -> None:
    """Second identical pull (every 30 min, most rows unchanged): no new
    rows, no rewrites, no duplicates."""
    await fill_records("member_support", _RECORDS)
    report = await fill_records("member_support", _RECORDS)
    assert report["calls_written"] == 0
    assert report["evals_filled"] == 0
    n = await pg_fill.fetchval("SELECT COUNT(*) FROM command_center.calls")
    assert n == 3  # webhook seed + 2 created — no duplicates


@pytest.mark.asyncio
async def test_dry_run_writes_nothing(pg_fill: asyncpg.Connection) -> None:
    report = await fill_records("member_support", _RECORDS, dry_run=True)
    assert report["calls_written"] == 2
    n = await pg_fill.fetchval(
        "SELECT COUNT(*) FROM command_center.calls "
        "WHERE dialpad_call_id IN ('CALL-NEW', 'CALL-PENDING')"
    )
    assert n == 0

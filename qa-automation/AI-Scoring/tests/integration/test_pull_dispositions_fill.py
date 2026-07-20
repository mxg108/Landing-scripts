"""C4 fill semantics against a real Postgres: stats_pull fills only rows
the webhook era predates (NULL guards), joins evals by the triple key,
and re-runs are idempotent."""

from __future__ import annotations

from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

from scripts.pull_dispositions import StatsRecord, fill_tables

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
    ):
        await clean_pg.execute((MIGRATIONS_DIR / name).read_text(encoding="utf-8"))
    monkeypatch.setenv("DATABASE_URL", pg_dsn)

    # CC calls: one already webhook-stamped, one pre-webhook (NULL source).
    await clean_pg.execute(
        """
        INSERT INTO command_center.calls
            (team_id, dialpad_call_id, seen_via,
             disposition_category, disposition, disposition_source)
        VALUES
            ('member_support', 'CALL-WEBHOOK', 'webhook',
             'Billing', 'Refund', 'webhook'),
            ('member_support', 'CALL-OLD', 'qa_backfill', NULL, NULL, NULL)
        """
    )
    # Evals: one already stamped at Stage 1, one pre-C3 (NULL), the latter
    # carrying the export's id in its ENTRY-POINT column (the usual case).
    await clean_pg.execute(
        f"""
        INSERT INTO qa.evaluations
            (team_id, agent_name_raw, state, source, models_used,
             dialpad_call_id, dialpad_entry_point_call_id,
             dialpad_disposition_category, dialpad_disposition)
        VALUES
            ('member_support', 'A', 'draft', 'ai', '{_MODELS}'::jsonb,
             'CALL-WEBHOOK', NULL, 'Billing', 'Refund'),
            ('member_support', 'B', 'draft', 'ai', '{_MODELS}'::jsonb,
             'LEG-OLD', 'CALL-OLD', NULL, NULL)
        """
    )
    return clean_pg


_RECORDS = [
    StatsRecord("CALL-WEBHOOK", "Access & Entry", "Lockout"),
    StatsRecord("CALL-OLD", "Access & Entry", "Smart-lock failure"),
    StatsRecord("CALL-UNKNOWN", "Billing", None),
    StatsRecord("CALL-NODISP", None, None),
]


@pytest.mark.asyncio
async def test_fill_respects_webhook_seam(pg_fill: asyncpg.Connection) -> None:
    report = await fill_tables("member_support", _RECORDS, dry_run=False)
    assert report["rows_in_export"] == 4
    assert report["with_disposition"] == 3
    assert report["cc_calls_filled"] == 1
    assert report["evals_filled"] == 1
    assert report["row_failures"] == 0

    # The webhook-stamped CC row is untouched.
    webhook_row = await pg_fill.fetchrow(
        "SELECT disposition_category, disposition, disposition_source "
        "FROM command_center.calls WHERE dialpad_call_id = 'CALL-WEBHOOK'"
    )
    assert webhook_row["disposition_category"] == "Billing"
    assert webhook_row["disposition_source"] == "webhook"

    # The pre-webhook row got the stats fill.
    old_row = await pg_fill.fetchrow(
        "SELECT disposition_category, disposition, disposition_source "
        "FROM command_center.calls WHERE dialpad_call_id = 'CALL-OLD'"
    )
    assert old_row["disposition_category"] == "Access & Entry"
    assert old_row["disposition"] == "Smart-lock failure"
    assert old_row["disposition_source"] == "stats_pull"

    # Eval joined via its entry-point id; the stamped one untouched.
    stamped = await pg_fill.fetchrow(
        "SELECT dialpad_disposition_category FROM qa.evaluations "
        "WHERE agent_name_raw = 'A'"
    )
    assert stamped["dialpad_disposition_category"] == "Billing"
    filled = await pg_fill.fetchrow(
        "SELECT dialpad_disposition_category, dialpad_disposition "
        "FROM qa.evaluations WHERE agent_name_raw = 'B'"
    )
    assert filled["dialpad_disposition_category"] == "Access & Entry"
    assert filled["dialpad_disposition"] == "Smart-lock failure"


@pytest.mark.asyncio
async def test_rerun_is_idempotent(pg_fill: asyncpg.Connection) -> None:
    await fill_tables("member_support", _RECORDS, dry_run=False)
    report = await fill_tables("member_support", _RECORDS, dry_run=False)
    assert report["cc_calls_filled"] == 0
    assert report["evals_filled"] == 0


@pytest.mark.asyncio
async def test_dry_run_writes_nothing(pg_fill: asyncpg.Connection) -> None:
    report = await fill_tables("member_support", _RECORDS, dry_run=True)
    assert report["cc_calls_filled"] == 1
    assert report["evals_filled"] == 1
    row = await pg_fill.fetchrow(
        "SELECT disposition_source FROM command_center.calls "
        "WHERE dialpad_call_id = 'CALL-OLD'"
    )
    assert row["disposition_source"] is None

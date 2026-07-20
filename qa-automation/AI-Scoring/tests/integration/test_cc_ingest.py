"""C2 checkpoint — replay synthetic webhook fixtures → exact rows.

Exercises command_center.services.store against a real Postgres with
migrations 004/005/006/016 applied: append-only webhook_events, calls
fold write-through, hold-cycle materialization, dedupe idempotency, and
the signed end-to-end path through the FastAPI route.
"""

from __future__ import annotations

import json
from pathlib import Path

import asyncpg
import jwt as pyjwt
import pytest
import pytest_asyncio

from command_center.services import store

REPO_ROOT = Path(__file__).resolve().parents[4]
MIGRATIONS_DIR = REPO_ROOT / "database" / "migrations"
FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures" / "cc_webhooks" / "call_lifecycle.json"
)

SECRET = "integration-test-subscription-secret-0123456789"


def _load_fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest_asyncio.fixture
async def pg_cc(clean_pg: asyncpg.Connection, pg_dsn: str, monkeypatch):
    """clean_pg + 004/005/006/016, with the CC store pool pointed at the
    container DB (the unit-suite autouse fixture deletes DATABASE_URL;
    re-point it here and reset the module pool on both sides)."""
    for name in (
        "004_create_schemas_and_teams.sql",
        "005_command_center_tables.sql",
        "006_qa_tables.sql",
        "016_cc_dispositions_ai_csat_holds.sql",
    ):
        await clean_pg.execute((MIGRATIONS_DIR / name).read_text(encoding="utf-8"))
    monkeypatch.setenv("DATABASE_URL", pg_dsn)
    store.reset_pool_for_tests()
    try:
        yield clean_pg
    finally:
        await store.close_pool()
        store.reset_pool_for_tests()


async def _replay(events: list[dict], team_id: str) -> list[str]:
    return [await store.ingest_event(team_id, e) for e in events]


@pytest.mark.asyncio
async def test_replay_lifecycle_exact_rows(pg_cc: asyncpg.Connection) -> None:
    fx = _load_fixture()
    statuses = await _replay(fx["events"], fx["team_id"])
    assert statuses == ["ingested"] * 6

    # webhook_events: 6 appended, all processed, payload verbatim.
    events = await pg_cc.fetch(
        "SELECT state, event_timestamp, processed_at, raw_payload "
        "FROM command_center.webhook_events ORDER BY id"
    )
    assert [r["state"] for r in events] == [
        "ringing", "connected", "hold", "connected", "hold", "hangup",
    ]
    assert all(r["processed_at"] is not None for r in events)
    assert json.loads(events[0]["raw_payload"]) == fx["events"][0]

    # calls: one row, folded to its final state.
    call = await pg_cc.fetchrow(
        "SELECT * FROM command_center.calls "
        "WHERE team_id = 'member_support' AND dialpad_call_id = 'DP-C2-1'"
    )
    assert call is not None
    assert call["dialpad_master_call_id"] == "DP-C2-M"
    assert call["dialpad_entry_point_call_id"] == "DP-C2-E"
    assert call["seen_via"] == "webhook"
    assert call["last_state"] == "hangup"
    assert call["direction"] == "inbound"
    assert call["external_number"] == "+15550100"
    assert call["agent_name"] == "Jane Agent"
    assert call["dialpad_agent_id"] == "8123"
    assert call["caller_name"] == "Pat Caller"
    assert call["total_duration_ms"] == 720000
    assert call["total_hold_seconds"] == 160
    assert call["disposition_category"] == "Access & Entry"
    assert call["disposition"] == "Smart-lock failure"
    assert call["disposition_source"] == "webhook"
    assert float(call["ai_csat"]) == 4.5
    assert call["started_at"] is not None
    assert call["connected_at"] is not None
    assert call["ended_at"] is not None

    # hold_intervals: the two materialized cycles, exact.
    holds = await pg_cc.fetch(
        "SELECT started_at, ended_at, seconds, ended_by "
        "FROM command_center.hold_intervals WHERE call_id = $1 "
        "ORDER BY started_at",
        call["id"],
    )
    assert [(r["seconds"], r["ended_by"]) for r in holds] == [
        (102, "connected"),
        (58, "hangup"),
    ]


@pytest.mark.asyncio
async def test_second_replay_is_idempotent(pg_cc: asyncpg.Connection) -> None:
    """Redelivery of the full sequence dedupes on the 005 partial UNIQUE —
    no new events, no double-fold, rollup unchanged."""
    fx = _load_fixture()
    await _replay(fx["events"], fx["team_id"])
    statuses = await _replay(fx["events"], fx["team_id"])
    assert statuses == ["duplicate"] * 6

    n_events = await pg_cc.fetchval(
        "SELECT COUNT(*) FROM command_center.webhook_events"
    )
    assert n_events == 6
    call = await pg_cc.fetchrow(
        "SELECT total_hold_seconds FROM command_center.calls "
        "WHERE dialpad_call_id = 'DP-C2-1'"
    )
    assert call["total_hold_seconds"] == 160
    n_holds = await pg_cc.fetchval(
        "SELECT COUNT(*) FROM command_center.hold_intervals"
    )
    assert n_holds == 2


@pytest.mark.asyncio
async def test_agent_status_event_appended_not_folded(
    pg_cc: asyncpg.Connection,
) -> None:
    """Nothing Dialpad hands us gets discarded: agent-status events land
    in webhook_events verbatim; the calls fold is call-events-only in C2."""
    status = await store.ingest_event("member_support", {
        "state": "available",
        "target": {"id": 8123, "type": "user"},
        "event_timestamp": 1784109600000,
    })
    assert status == "appended"
    row = await pg_cc.fetchrow(
        "SELECT event_kind, dialpad_agent_id, state "
        "FROM command_center.webhook_events"
    )
    assert row["event_kind"] == "agent_status"
    assert row["dialpad_agent_id"] == "8123"
    n_calls = await pg_cc.fetchval("SELECT COUNT(*) FROM command_center.calls")
    assert n_calls == 0


@pytest.mark.asyncio
async def test_route_end_to_end_signed(pg_cc: asyncpg.Connection, monkeypatch) -> None:
    """The full receiver path: JWT-signed body → verify → team resolve →
    ingest. Tampered bodies bounce with 401 before any storage."""
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from command_center.routes.webhooks import router

    monkeypatch.setenv("DIALPAD_WEBHOOK_SECRET", SECRET)
    app = FastAPI()
    app.include_router(router)

    fx = _load_fixture()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://cc.test"
    ) as client:
        for event in fx["events"]:
            token = pyjwt.encode(event, SECRET, algorithm="HS256")
            resp = await client.post("/api/webhooks/dialpad", content=token)
            assert resp.status_code == 200
            assert resp.json() == {"status": "ingested"}

        tampered = pyjwt.encode(fx["events"][0], "wrong-secret", algorithm="HS256")
        resp = await client.post("/api/webhooks/dialpad", content=tampered)
        assert resp.status_code == 401

    n_events = await pg_cc.fetchval(
        "SELECT COUNT(*) FROM command_center.webhook_events"
    )
    assert n_events == 6
    call = await pg_cc.fetchrow(
        "SELECT disposition_category, total_hold_seconds "
        "FROM command_center.calls WHERE dialpad_call_id = 'DP-C2-1'"
    )
    assert call["disposition_category"] == "Access & Entry"
    assert call["total_hold_seconds"] == 160

"""F3 — Sheets↔Postgres read-path factory dispatch (ReadPathFlip §5).

Verifies get_provider selects by QA_READ_PATH, connect()s the Postgres
provider before handing it out, caches within a mode, rebuilds + closes
the old instance on a flag flip, and that close_all_providers tears down
pooled providers. Both providers are faked — no gspread creds, no live
Postgres — so this pins the factory wiring in isolation.
"""

from __future__ import annotations

import asyncio

import pytest

import backend.services.data_provider as dp


class _FakeSheets:
    name = "Google Sheets"

    def __init__(self, config=None):
        self.config = config
        self.closed = False
    # no close() — SheetsProvider has none; factory must tolerate that.


class _FakePg:
    name = "PostgreSQL"

    def __init__(self, config=None):
        self.config = config
        self.connected = False
        self.closed = False

    async def connect(self):
        self.connected = True

    async def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _reset_and_fake(monkeypatch):
    """Isolate each test: fresh caches, faked provider classes, no flag."""
    monkeypatch.setattr(dp, "_providers", {})
    monkeypatch.setattr(dp, "_provider_modes", {})
    monkeypatch.delenv("QA_READ_PATH", raising=False)
    import backend.services.history_service as hs
    import backend.services.db_provider as db
    monkeypatch.setattr(hs, "SheetsProvider", _FakeSheets)
    monkeypatch.setattr(db, "PostgresProvider", _FakePg)


def test_default_mode_returns_sheets():
    p = asyncio.run(dp.get_provider("sales"))
    assert isinstance(p, _FakeSheets)
    assert dp._provider_modes["sales"] == "sheets"


def test_postgres_mode_connects_before_return(monkeypatch):
    monkeypatch.setenv("QA_READ_PATH", "postgres")
    p = asyncio.run(dp.get_provider("sales"))
    assert isinstance(p, _FakePg)
    assert p.connected is True, "provider must be connect()-ed before hand-off"


def test_caches_within_mode(monkeypatch):
    monkeypatch.setenv("QA_READ_PATH", "postgres")

    async def _twice():
        a = await dp.get_provider("sales")
        b = await dp.get_provider("sales")
        return a, b

    a, b = asyncio.run(_twice())
    assert a is b  # same cached instance, connect() ran once


def test_flag_flip_rebuilds_and_closes_previous(monkeypatch):
    monkeypatch.setenv("QA_READ_PATH", "postgres")
    pg = asyncio.run(dp.get_provider("sales"))
    assert isinstance(pg, _FakePg)

    monkeypatch.setenv("QA_READ_PATH", "sheets")
    sheets = asyncio.run(dp.get_provider("sales"))
    assert isinstance(sheets, _FakeSheets)
    assert pg.closed is True, "superseded Postgres pool must be closed on flip"
    assert dp._provider_modes["sales"] == "sheets"


def test_close_all_providers_tears_down_pool(monkeypatch):
    monkeypatch.setenv("QA_READ_PATH", "postgres")
    pg = asyncio.run(dp.get_provider("sales"))
    asyncio.run(dp.close_all_providers())
    assert pg.closed is True
    assert dp._providers == {} and dp._provider_modes == {}


def test_close_all_tolerates_closeless_sheets():
    asyncio.run(dp.get_provider("sales"))          # sheets path, no close()
    asyncio.run(dp.close_all_providers())          # must not raise
    assert dp._providers == {}

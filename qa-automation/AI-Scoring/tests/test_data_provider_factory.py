"""Provider factory — always-Postgres since F5 (ReadPathFlip §5).

The QA_READ_PATH flag and Sheets/shadow branches were deleted after the
2026-07-15 flip; get_provider builds one connect()-ed PostgresProvider per
team, caches it, and close_all_providers tears the pools down at shutdown.
PostgresProvider is faked — no live DB.
"""

from __future__ import annotations

import asyncio

import pytest

import backend.services.data_provider as dp


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


class _CloselessProvider:
    """A provider without close() — close_all must tolerate it."""
    name = "closeless"


@pytest.fixture(autouse=True)
def _reset_and_fake(monkeypatch):
    monkeypatch.setattr(dp, "_providers", {})
    import backend.services.db_provider as db
    monkeypatch.setattr(db, "PostgresProvider", _FakePg)


def test_returns_connected_postgres_provider():
    p = asyncio.run(dp.get_provider("sales"))
    assert isinstance(p, _FakePg)
    assert p.connected is True, "provider must be connect()-ed before hand-off"


def test_caches_per_team():
    async def _calls():
        a = await dp.get_provider("sales")
        b = await dp.get_provider("sales")
        c = await dp.get_provider("member_support")
        return a, b, c

    a, b, c = asyncio.run(_calls())
    assert a is b                    # same cached instance, connect() once
    assert c is not a                # per-team instances
    assert set(dp._providers) == {"sales", "member_support"}


def test_close_all_providers_tears_down_pools():
    p = asyncio.run(dp.get_provider("sales"))
    asyncio.run(dp.close_all_providers())
    assert p.closed is True
    assert dp._providers == {}


def test_close_all_tolerates_closeless_provider():
    dp._providers["x"] = _CloselessProvider()
    asyncio.run(dp.close_all_providers())     # must not raise
    assert dp._providers == {}

"""W6 — indexed /datapoints/{call_id} lookup (ReadPathFlip §3, slice F5).

PostgresProvider.get_by_eval_id replaces the 365-day get_all_history load
+ Python scan with a single indexed row hit, but must never return a
DIFFERENT record than the scan would: it verifies the resolved row's
link-derived eval_id equals call_id, else returns None so the route falls
back to the scan. All DB access is faked; no live Postgres.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from backend.services.data_provider import DataProvider
from backend.services.db_provider import PostgresProvider
from backend.services.history_service import _extract_eval_id
from tests.conftest import load_test_config


class _Conn:
    def __init__(self, eval_rows, section_rows):
        self.eval_rows = list(eval_rows)
        self.section_rows = list(section_rows)
        self.queries: list[str] = []

    async def fetch(self, query, *params):
        self.queries.append(query)
        if "FROM qa.evaluation_sections" in query:
            return [s for s in self.section_rows if s["evaluation_id"] == params[0]]
        if "FROM qa.agents" in query:
            return []
        raise AssertionError(f"unexpected fetch: {query}")

    async def fetchrow(self, query, *params):
        self.queries.append(query)
        _team_id, call_id = params
        # Model the id-column probe as "the link's trailing segment matches".
        for ev in self.eval_rows:
            if _extract_eval_id(ev.get("dialpad_link") or "") == call_id:
                return ev
        return None

    async def fetchval(self, query, *params):
        return 1


class _Acquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _Pool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _Acquire(self._conn)


def _eval_row(eval_id=1, link="https://dialpad.com/launchpad/call/CALL123", **over):
    row = {
        "id": eval_id, "agent_name_raw": "Jane Doe", "agent_email": "jane@landing.com",
        "evaluator_email": "boss@landing.com",
        "ts": datetime(2026, 7, 2, 15, 30, tzinfo=timezone.utc),
        "overall_score": 87.5, "dialpad_link": link, "key_strengths": "",
        "opportunities": "", "call_summary": "", "caller_name": "", "caller_phone": "",
        "source": "ai",
    }
    row.update(over)
    return row


def _sections(config, eval_id=1):
    rows = []
    for sec in config.sections_by_number:
        binaryish = "binary" in str(sec.score_type) or str(sec.score_type) == "auto_value"
        rows.append({
            "evaluation_id": eval_id, "section_id": sec.id,
            "numeric_score": None if binaryish else 4,
            "binary_value": "Y" if binaryish else None,
            "confidence": "high", "reasoning": f"r-{sec.id}",
        })
    return rows


def _provider(eval_rows, section_rows):
    config = load_test_config("sales_lite")
    p = PostgresProvider(config=config)
    p._roster = [["Name", "Email", "Supervisor", "Canonical Name"]]  # non-stale
    import time
    p._roster_at = time.monotonic()
    p._pool = _Pool(_Conn(eval_rows, section_rows))
    return p


def test_lookup_hit_uses_indexed_column_and_returns_record():
    config = load_test_config("sales_lite")
    p = _provider([_eval_row()], _sections(config))
    rec = asyncio.run(p.get_by_eval_id("CALL123"))
    assert rec is not None
    assert rec.eval_id == "CALL123"
    assert rec.agent_name == "Jane Doe"
    # the probe hit the entry-point id column (the W6 index target)
    probe = next(q for q in p._pool._conn.queries if "FROM qa.evaluations" in q)
    assert "dialpad_entry_point_call_id" in probe


def test_lookup_miss_returns_none():
    config = load_test_config("sales_lite")
    p = _provider([_eval_row()], _sections(config))
    assert asyncio.run(p.get_by_eval_id("NOPE")) is None


def test_lookup_verifies_link_derived_id_matches():
    """A row whose stored id matched but whose LINK parses to a different
    eval_id must NOT be returned — the route falls back to the scan so the
    result is byte-identical to today."""
    config = load_test_config("sales_lite")
    # Fake conn matches on the link segment; give it a row whose link says
    # OTHER while we ask for CALL123 → fetchrow returns None here, but the
    # verify guard is what protects the real id-column-match case. Assert
    # the guard directly: a returned row with a mismatched link yields None.
    p = _provider([_eval_row(link="https://dialpad.com/launchpad/call/OTHER")],
                  _sections(config))

    # Force fetchrow to return the OTHER-link row for ANY call_id, so only
    # the eval_id verify can reject it.
    conn = p._pool._conn
    other_row = conn.eval_rows[0]

    async def _always(query, *params):
        conn.queries.append(query)
        return other_row
    conn.fetchrow = _always

    assert asyncio.run(p.get_by_eval_id("CALL123")) is None


def test_empty_call_id_short_circuits():
    p = _provider([_eval_row()], [])
    assert asyncio.run(p.get_by_eval_id("")) is None
    # no DB query issued
    assert p._pool._conn.queries == []


def test_base_provider_default_is_none():
    class _Stub(DataProvider):
        async def list_agents(self):
            return []
        async def get_agent_history(self, agent_name, days=30):
            return []
        async def get_all_history(self, days=90):
            return []
        def _get_mails_sheet(self):
            return []

    assert asyncio.run(_Stub().get_by_eval_id("anything")) is None

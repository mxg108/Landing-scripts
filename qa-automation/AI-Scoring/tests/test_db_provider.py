"""PostgresProvider — contract parity with SheetsProvider.

The provider swap at the read-path flip only works if both providers
emit identical EvaluationRecord shapes: sections keyed by history_id,
the manager_email/evaluator_email and improvements/opportunities naming
bridges, YN_DISPLAY score strings, tz-aware UTC timestamps.

All DB access goes through a fake asyncpg pool — the autouse
_no_real_database fixture guarantees no DSN is present anyway.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import backend.services.db_provider as db_provider
from backend.services.db_provider import PostgresProvider, _display_score
from backend.services.history_service import (
    _agent_email_for_name_in_rows,
    _email_in_mails_rows,
)
from tests.conftest import load_test_config


# ---------------------------------------------------------------------------
# Fake asyncpg pool
# ---------------------------------------------------------------------------

class FakeConn:
    def __init__(self, eval_rows, section_rows, agent_rows):
        self.eval_rows = eval_rows
        self.section_rows = section_rows
        self.agent_rows = agent_rows
        self.queries: list[str] = []

    async def fetch(self, query, *params):
        self.queries.append(query)
        if "FROM qa.evaluation_sections" in query:
            wanted = set(params[0])
            return [s for s in self.section_rows if s["evaluation_id"] in wanted]
        if "FROM qa.evaluations" in query:
            return self.eval_rows
        if "FROM qa.agents" in query:
            return self.agent_rows
        raise AssertionError(f"unexpected query: {query}")

    async def fetchval(self, query):
        return 1


class _FakeAcquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _FakeAcquire(self._conn)


def make_provider(eval_rows=(), section_rows=(), agent_rows=()):
    provider = PostgresProvider(config=load_test_config("sales_lite"))
    provider._pool = FakePool(FakeConn(list(eval_rows), list(section_rows), list(agent_rows)))
    return provider


def make_eval_row(eval_id=1, **overrides):
    row = {
        "id": eval_id,
        "agent_name_raw": "Jane Doe",
        "agent_email": "jane@landing.com",
        "evaluator_email": "boss@landing.com",
        "ts": datetime(2026, 7, 2, 15, 30, tzinfo=timezone.utc),
        "overall_score": 87.5,
        "dialpad_link": "https://dialpad.com/launchpad/call/12345",
        "key_strengths": "warm greeting",
        "opportunities": "slow wrap-up",
        "call_summary": "billing question",
        "caller_name": "Bob",
        "caller_phone": "+15550001111",
        "source": "ai",
    }
    row.update(overrides)
    return row


def make_section_rows(config, eval_id=1):
    """One DB row per configured section, shaped by its score_type."""
    rows = []
    for sec in config.sections_by_number:
        binaryish = "binary" in str(sec.score_type) or str(sec.score_type) == "auto_value"
        rows.append({
            "evaluation_id": eval_id,
            "section_id": sec.id,
            "numeric_score": None if binaryish else 4,
            "binary_value": "Y" if binaryish else None,
            "confidence": "high",
            "reasoning": f"reasoning for {sec.id}",
        })
    return rows


# ---------------------------------------------------------------------------
# _display_score — sheet-cell rendering parity
# ---------------------------------------------------------------------------

def test_display_score_missing_row_is_blank_cell():
    assert _display_score(None) == ""


def test_display_score_numeric_renders_digit_string():
    assert _display_score({"numeric_score": 4, "binary_value": None}) == "4"


def test_display_score_binary_renders_via_yn_display():
    assert _display_score({"numeric_score": None, "binary_value": "Y"}) == "Yes"
    assert _display_score({"numeric_score": None, "binary_value": "N"}) == "No"
    assert _display_score({"numeric_score": None, "binary_value": "NA"}) == "Not Applicable"


def test_display_score_empty_row_is_blank():
    assert _display_score({"numeric_score": None, "binary_value": None}) == ""


# ---------------------------------------------------------------------------
# Record shape parity
# ---------------------------------------------------------------------------

def test_sections_keyed_by_history_id():
    provider = make_provider()
    config = provider._config
    rec = provider._record_from_rows(make_eval_row(), make_section_rows(config))
    assert set(rec.sections) == {s.history_id for s in config.sections_by_number}


def test_naming_bridges_and_field_mapping():
    provider = make_provider()
    rec = provider._record_from_rows(make_eval_row(), [])
    assert rec.agent_name == "Jane Doe"
    assert rec.manager_email == "boss@landing.com"   # ← evaluator_email
    assert rec.improvements == "slow wrap-up"        # ← opportunities
    assert rec.eval_id == "12345"                    # trailing link segment
    assert rec.overall_score == 87.5
    assert rec.source == "ai"


def test_unscored_configured_section_reads_as_blank_cell():
    provider = make_provider()
    config = provider._config
    rows = make_section_rows(config)[1:]  # drop the first section's row
    rec = provider._record_from_rows(make_eval_row(), rows)
    first = config.sections_by_number[0]
    assert rec.sections[first.history_id].score == ""
    assert rec.sections[first.history_id].confidence is None


def test_section_rows_outside_active_rubric_are_dropped():
    provider = make_provider()
    rows = [{
        "evaluation_id": 1, "section_id": "retired_section",
        "numeric_score": 3, "binary_value": None,
        "confidence": None, "reasoning": None,
    }]
    rec = provider._record_from_rows(make_eval_row(), rows)
    assert "retired_section" not in rec.sections


def test_naive_timestamp_tagged_utc():
    provider = make_provider()
    rec = provider._record_from_rows(
        make_eval_row(ts=datetime(2026, 7, 2, 15, 30)), []
    )
    assert rec.timestamp.tzinfo is not None
    assert rec.timestamp.utcoffset().total_seconds() == 0


# ---------------------------------------------------------------------------
# Query surface
# ---------------------------------------------------------------------------

def test_history_query_is_team_scoped_and_finalized_only():
    provider = make_provider(
        eval_rows=[make_eval_row()],
        section_rows=make_section_rows(load_test_config("sales_lite")),
    )
    records = asyncio.run(provider.get_agent_history("Jane Doe", days=30))
    assert len(records) == 1
    eval_query = next(
        q for q in provider._pool._conn.queries if "FROM qa.evaluations" in q
    )
    assert "team_id = $1" in eval_query
    assert "state = 'finalized'" in eval_query
    assert "LOWER(agent_name_raw)" in eval_query


def test_all_history_query_has_no_agent_filter():
    provider = make_provider(eval_rows=[], section_rows=[])
    records = asyncio.run(provider.get_all_history(days=90))
    assert records == []
    eval_query = next(
        q for q in provider._pool._conn.queries if "FROM qa.evaluations" in q
    )
    assert "LOWER(agent_name_raw)" not in eval_query


def test_no_stale_qa_scoring_schema_references():
    src = Path(db_provider.__file__).read_text(encoding="utf-8")
    assert "qa_scoring" not in src


# ---------------------------------------------------------------------------
# Roster (qa.agents → Mails-shaped rows)
# ---------------------------------------------------------------------------

def test_get_mails_sheet_requires_connect():
    provider = make_provider()
    try:
        provider._get_mails_sheet()
        raise AssertionError("expected RuntimeError before roster load")
    except RuntimeError:
        pass


def test_roster_rows_work_with_history_service_helpers():
    agent_rows = [{
        "name": "jane doe", "email": "jane@landing.com",
        "supervisor_email": "boss@landing.com", "canonical_name": "Jane Doe",
    }]
    provider = make_provider(agent_rows=agent_rows)
    asyncio.run(provider._refresh_roster())
    rows = provider._get_mails_sheet()
    assert rows[0] == ["Name", "Email", "Supervisor", "Canonical Name"]
    assert _email_in_mails_rows("JANE@landing.com", rows)
    assert _agent_email_for_name_in_rows("Jane Doe", rows) == "jane@landing.com"


# ---------------------------------------------------------------------------
# Call-metadata fields (DispositionDesign §5 / PulpoConnection §4.2)
# ---------------------------------------------------------------------------

def test_call_metadata_fields_flow_through_record():
    """/datapoint reads disposition, CSAT, clocks, the id triple, and the
    pulpo_docs footnotes off the record — asyncpg hands JSONB back as a
    JSON string, so the builder must parse it."""
    provider = make_provider()
    row = make_eval_row(
        dialpad_disposition_category="Unit Issues",
        dialpad_disposition="Lockouts",
        ai_csat=4.5,
        call_duration_ms=312_000,
        dialpad_call_id="111",
        dialpad_entry_point_call_id="222",
        dialpad_master_call_id="333",
        dialpad_call_metadata=(
            '{"sop_used": "Lockout SOP", "pulpo_docs": ['
            '{"id": "a", "title": "Lockout SOP", "score": 0.91}]}'
        ),
    )
    rec = provider._record_from_rows(row, make_section_rows(provider._config))
    assert rec.dialpad_disposition_category == "Unit Issues"
    assert rec.dialpad_disposition == "Lockouts"
    assert rec.ai_csat == 4.5
    assert rec.call_duration_ms == 312_000
    assert rec.dialpad_call_id == "111"
    assert rec.dialpad_entry_point_call_id == "222"
    assert rec.dialpad_master_call_id == "333"
    assert rec.sop_used == "Lockout SOP"
    assert rec.pulpo_docs == [{"id": "a", "title": "Lockout SOP", "score": 0.91}]


def test_call_metadata_absent_defaults_are_benign():
    """Rows scored before the CC/Pulpo era (and the SheetsProvider parity
    path) leave every metadata field at its default — never raise."""
    provider = make_provider()
    rec = provider._record_from_rows(
        make_eval_row(), make_section_rows(provider._config)
    )
    assert rec.dialpad_disposition_category is None
    assert rec.ai_csat is None
    assert rec.sop_used is None
    assert rec.pulpo_docs == []

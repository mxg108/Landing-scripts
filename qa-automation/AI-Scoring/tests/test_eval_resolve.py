"""ScorecardActionsDesign §3 (S1) — DB-backed action resolution.

`resolve_evaluation` is the seam every scorecard action stands on after
the in-memory `_jobs` dict dies with the process. These tests pin the
job-id parsing, the two-column Dialpad-id probe, and the DB-row →
Stage-1-draft-dump section reshaping against a stub pool (same FakeConn
pattern as test_eval_store — no real DB, per Wave2Plan Phase 1).
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

import pytest

from backend.services import eval_store
from backend.services.eval_store import call_id_from_job_id, resolve_evaluation


# ---------------------------------------------------------------------------
# call_id_from_job_id
# ---------------------------------------------------------------------------

class TestCallIdFromJobId:

    def test_job_id_with_agent_suffix(self):
        assert call_id_from_job_id("5035229460504576_Juan_Celso") == "5035229460504576"

    def test_bare_call_id_passes_through(self):
        assert call_id_from_job_id("5035229460504576") == "5035229460504576"

    def test_agent_name_with_many_underscores(self):
        # _make_job_id flattens spaces, so multi-word names produce
        # multiple underscores — only the first split matters.
        assert call_id_from_job_id("42_Maria_de_la_Cruz") == "42"

    def test_non_numeric_prefix_returns_empty(self):
        # Junk ids skip the DB probe entirely.
        assert call_id_from_job_id("Juan_Celso") == ""

    def test_empty_returns_empty(self):
        assert call_id_from_job_id("") == ""


# ---------------------------------------------------------------------------
# resolve_evaluation
# ---------------------------------------------------------------------------

def _eval_row(**overrides):
    row = {
        "id": 2377,
        "team_id": "member_support",
        "state": "draft",
        "source": "ai",
        "scoring_status": "flagged_human_review",
        "agent_name_raw": "Juan Celso",
        "agent_email": "juan.celso@hellolanding.com",
        "evaluator_email": None,
        "dialpad_call_id": "5035229460504576",
        "dialpad_entry_point_call_id": "6105002063634432",
        "dialpad_link": "https://dialpad.com/callhistory/callreview/6105002063634432",
        "call_summary": "Member called about billing",
        "key_strengths": "Good tone",
        "opportunities": "Faster holds",
        "overall_score": None,
        "models_used": json.dumps(
            {"text": {"provider": "gemini", "model": "gemini-2.5-flash"}}
        ),
        "agent_id": 7,
        "duration_ms": 180000,
    }
    row.update(overrides)
    return row


def _section_rows():
    return [
        {"section_id": "greeting", "score_type": "numeric", "numeric_score": 5,
         "binary_value": None, "score_source": "ai", "confidence": "HIGH",
         "reasoning": "warm open"},
        {"section_id": "caller_id", "score_type": "binary", "numeric_score": None,
         "binary_value": "Y", "score_source": "ai", "confidence": "MED",
         "reasoning": "verified"},
        # migration-012 numeric-NA shape: numeric section, binary_value='NA'
        {"section_id": "efficiency", "score_type": "numeric", "numeric_score": None,
         "binary_value": "NA", "score_source": "ai", "confidence": "LOW",
         "reasoning": "no holds to judge"},
        # non-AI rows are regenerated from config at approval — filtered out
        {"section_id": "human_review_required", "score_type": "manual_binary",
         "numeric_score": None, "binary_value": "NA",
         "score_source": "manual_default", "confidence": None, "reasoning": None},
        {"section_id": "documentation", "score_type": "auto_value",
         "numeric_score": None, "binary_value": "Y",
         "score_source": "auto_value", "confidence": None, "reasoning": None},
    ]


class FakeConn:
    def __init__(self, row, sections):
        self.row = row
        self.sections = sections
        self.fetchrow_args = None

    async def fetchrow(self, query, *args):
        self.fetchrow_args = (query, args)
        return self.row

    async def fetch(self, query, *args):
        return self.sections


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


@pytest.mark.asyncio
class TestResolveEvaluation:

    @pytest.fixture(autouse=True)
    def _stub_pool(self, monkeypatch):
        self.conn = FakeConn(_eval_row(), _section_rows())

        async def fake_get_pool():
            return FakePool(self.conn)

        monkeypatch.setattr(eval_store, "_get_pool", fake_get_pool)

    async def test_resolves_by_job_id_and_probes_both_id_columns(self):
        ref = await resolve_evaluation("member_support", "5035229460504576_Juan_Celso")
        assert ref is not None
        assert ref.id == 2377
        assert ref.state == "draft"
        assert ref.scoring_status == "flagged_human_review"
        assert ref.model == "gemini-2.5-flash"
        # S4 fields — series-rebuild target and long-call-flag parity.
        assert ref.agent_id == 7
        assert ref.duration_ms == 180000.0
        assert isinstance(ref.duration_ms, float)
        # The 2026-07-24 incident guarantee: one probe, both columns.
        query, args = self.conn.fetchrow_args
        assert "dialpad_entry_point_call_id = $2" in query
        assert "dialpad_call_id = $2" in query
        assert args == ("member_support", "5035229460504576")

    async def test_sections_reshaped_to_draft_dump(self):
        ref = await resolve_evaluation("member_support", "5035229460504576")
        by_id = {s["id"]: s for s in ref.sections}
        # AI rows only — manual_default and auto_value rows are excluded.
        assert set(by_id) == {"greeting", "caller_id", "efficiency"}
        assert by_id["greeting"] == {
            "id": "greeting", "name": "greeting", "score": 5,
            "score_type": "numeric", "yn_value": None, "confidence": "high",
            "reasoning": "warm open", "audio_dependent": False, "flags": [],
        }
        assert by_id["caller_id"]["score_type"] == "yn"
        assert by_id["caller_id"]["score"] is None
        assert by_id["caller_id"]["yn_value"] == "Y"
        assert by_id["caller_id"]["confidence"] == "medium"
        # numeric NA keeps the (score=None, yn_value='NA') draft shape.
        assert by_id["efficiency"]["score"] is None
        assert by_id["efficiency"]["yn_value"] == "NA"
        assert by_id["efficiency"]["score_type"] == "numeric"

    async def test_models_used_accepts_dict_codec(self):
        # asyncpg with a JSONB codec hands back a dict, not a str.
        self.conn.row = _eval_row(models_used={"text": {"model": "gemma-4-27b-q4"}})
        ref = await resolve_evaluation("member_support", "5035229460504576")
        assert ref.model == "gemma-4-27b-q4"

    async def test_overall_score_numeric_to_float(self):
        self.conn.row = _eval_row(state="finalized", overall_score=85)
        ref = await resolve_evaluation("member_support", "5035229460504576")
        assert ref.overall_score == 85.0
        assert isinstance(ref.overall_score, float)

    async def test_miss_returns_none(self):
        self.conn.row = None
        assert await resolve_evaluation("member_support", "999") is None

    async def test_non_numeric_job_id_skips_probe(self):
        assert await resolve_evaluation("member_support", "Juan_Celso") is None
        assert self.conn.fetchrow_args is None

    async def test_no_pool_returns_none(self, monkeypatch):
        async def no_pool():
            return None
        monkeypatch.setattr(eval_store, "_get_pool", no_pool)
        assert await resolve_evaluation("member_support", "5035229460504576") is None

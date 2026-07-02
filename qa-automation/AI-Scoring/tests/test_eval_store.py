"""Stage 1 dual-write tests — Wave 2 Phase 4a.

The pure builders (scorecard → qa.evaluations dict + qa.evaluation_sections
models) carry most of the correctness weight; the upsert flow and the §7.3
swallow-everything contract are exercised against a stub pool/connection.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest

from backend.models.scorecard import ScorecardSection, ScorecardWithMeta
from backend.services import eval_store
from backend.services.eval_store import (
    build_draft_row,
    build_draft_sections,
    record_draft_evaluation,
)
from tests.conftest import load_test_config


@pytest.fixture(scope="module")
def ms_config():
    return load_test_config("member_support")


def _ai_section(sec_id, name, *, score=None, yn=None, score_type="numeric",
                confidence="high", reasoning="because", flags=None):
    return ScorecardSection(
        id=sec_id, name=name, score=score, yn_value=yn, score_type=score_type,
        confidence=confidence, reasoning=reasoning, flags=flags or [],
    )


def _scorecard(ms_config, **overrides):
    """A full MS draft: 8 AI-scored sections (manual + auto get no AI output)."""
    sections = [
        _ai_section("greeting", "Greeting", score=5),
        _ai_section("caller_id", "Caller ID", yn="Y", score_type="yn", confidence="medium"),
        _ai_section("purpose", "Purpose", score=4),
        _ai_section("matching", "Matching", score=4, confidence="low"),
        _ai_section("process_adherence", "Process", score=5),
        _ai_section("call_resolution", "Resolution", score=3),
        _ai_section("comms", "Communication", score=4),
        _ai_section("efficiency", "Efficiency", score=2, flags=["long_hold"]),
    ]
    defaults = dict(
        sections=sections,
        key_strengths="Good tone",
        opportunities="Faster holds",
        call_summary="Member called about billing",
        call_id="DP123",
        agent_name="Jane Agent",
        manager_email="lead@landing.com",
        dialpad_link="https://dialpad.test/call/DP123",
        duration_ms=354000.0,
        model="gemini-2.5-flash",
        sop_used="Billing SOP",
        caller_name="Pat Caller",
        caller_phone="+15550100",
        call_started_at_utc=datetime(2026, 7, 2, 15, 30, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    return ScorecardWithMeta(**defaults)


# ---------------------------------------------------------------------------
# build_draft_row
# ---------------------------------------------------------------------------

class TestBuildDraftRow:

    def test_stage1_row_shape(self, ms_config):
        row = build_draft_row(_scorecard(ms_config), ms_config)
        assert row["state"] == "draft"
        assert row["source"] == "ai"
        assert row["team_id"] == "member_support"
        assert row["dialpad_call_id"] == "DP123"
        assert row["call_duration_ms"] == 354000
        assert row["ai_provider_primary"] == "gemini"
        assert row["scoring_status"] == "complete"

    def test_versions_not_stamped_at_draft_time(self, ms_config):
        """§3.6 — formula/rubric versions are stamped at score-compute time
        (Stage 2 post-cutover), never at draft."""
        row = build_draft_row(_scorecard(ms_config), ms_config)
        assert "formula_version" not in row
        assert "rubric_version" not in row
        assert "overall_score" not in row

    def test_models_used_shape(self, ms_config):
        row = build_draft_row(_scorecard(ms_config), ms_config)
        assert json.loads(row["models_used"]) == {
            "text": {"provider": "gemini", "model": "gemini-2.5-flash"}
        }

    def test_flagged_long_call_sets_scoring_status(self, ms_config):
        row = build_draft_row(_scorecard(ms_config, flagged_long_call=True), ms_config)
        assert row["scoring_status"] == "flagged_long_call"

    def test_section_flags_collected_into_metadata(self, ms_config):
        meta = json.loads(build_draft_row(_scorecard(ms_config), ms_config)["dialpad_call_metadata"])
        assert meta["stage1_flags"] == ["long_hold"]
        assert meta["sop_used"] == "Billing SOP"


# ---------------------------------------------------------------------------
# build_draft_sections
# ---------------------------------------------------------------------------

class TestBuildDraftSections:

    def test_ms_draft_has_nine_rows(self, ms_config):
        """8 AI sections + human_review_required NA-default; MS has no
        auto_value sections; cri got no AI output in this fixture? — no:
        cri IS in the rubric but absent from the scorecard, so no row."""
        rows = build_draft_sections(_scorecard(ms_config), ms_config)
        by_id = {r.section_id: r for r in rows}
        assert len(rows) == 9
        assert "cri" not in by_id  # no AI output → no draft row

    def test_numeric_ai_section(self, ms_config):
        rows = build_draft_sections(_scorecard(ms_config), ms_config)
        greeting = next(r for r in rows if r.section_id == "greeting")
        assert greeting.score_type == "numeric"
        assert greeting.numeric_score == 5
        assert greeting.binary_value is None
        assert greeting.score_source == "ai"
        assert greeting.ai_provider == "gemini"
        assert greeting.model == "gemini-2.5-flash"
        assert greeting.confidence == "HIGH"

    def test_binary_ai_section_and_confidence_map(self, ms_config):
        rows = build_draft_sections(_scorecard(ms_config), ms_config)
        caller = next(r for r in rows if r.section_id == "caller_id")
        assert caller.score_type == "binary"
        assert caller.binary_value == "Y"
        assert caller.numeric_score is None
        assert caller.confidence == "MED"

    def test_manual_na_default_row(self, ms_config):
        """human_review_required — the §3.8-point-7 / migration-012 shape."""
        rows = build_draft_sections(_scorecard(ms_config), ms_config)
        hrr = next(r for r in rows if r.section_id == "human_review_required")
        assert hrr.score_type == "manual_numeric"
        assert hrr.binary_value == "NA"
        assert hrr.numeric_score is None
        assert hrr.score_source == "manual_default"
        assert hrr.ai_provider is None

    def test_binary_na_from_ai(self, ms_config):
        sc = _scorecard(ms_config)
        sc.sections[1] = _ai_section("caller_id", "Caller ID", yn="NA", score_type="yn")
        caller = next(
            r for r in build_draft_sections(sc, ms_config) if r.section_id == "caller_id"
        )
        assert caller.score_type == "binary"
        assert caller.binary_value == "NA"

    def test_numeric_na_from_ai_uses_migration_012_shape(self, ms_config):
        """An NA on a numeric na_applicable section (efficiency is not —
        use a matching-shaped override) lands as numeric + binary_value NA."""
        sc = _scorecard(ms_config)
        sc.sections[7] = ScorecardSection(
            id="efficiency", name="Efficiency", score=None, yn_value="NA",
            score_type="numeric", confidence="low", reasoning="silent call",
        )
        eff = next(
            r for r in build_draft_sections(sc, ms_config) if r.section_id == "efficiency"
        )
        assert eff.score_type == "numeric"
        assert eff.numeric_score is None
        assert eff.binary_value == "NA"
        assert eff.score_source == "ai"

    def test_auto_value_sections_write_auto_rows(self):
        """Sales Q18 screen_recording: auto_value='Yes' → binary Y row."""
        sales = load_test_config("sales")
        sc = ScorecardWithMeta(
            sections=[], key_strengths="", opportunities="",
            model="gemini-2.5-flash",
        )
        rows = build_draft_sections(sc, sales)
        auto = [r for r in rows if r.score_source == "auto_value"]
        assert auto, "sales config has an auto_value section"
        assert all(r.binary_value == "Y" for r in auto)


# ---------------------------------------------------------------------------
# record_draft_evaluation — upsert + §7.3 swallow
# ---------------------------------------------------------------------------

class FakeConn:
    def __init__(self, existing_id=None):
        self.existing_id = existing_id
        self.inserts: list[tuple] = []
        self.updates: list[tuple] = []
        self.deletes: list[tuple] = []

    @asynccontextmanager
    async def transaction(self):
        yield

    async def fetchval(self, query, *args):
        if query.startswith("SELECT id"):
            return self.existing_id
        assert query.startswith("INSERT INTO qa.evaluations")
        self.inserts.append(("evaluations", args))
        return 101

    async def execute(self, query, *args):
        if query.startswith("UPDATE"):
            self.updates.append(args)
        elif query.startswith("DELETE"):
            self.deletes.append(args)
        else:
            self.inserts.append(("sections", args))


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


class TestRecordDraftEvaluation:
    pytestmark = pytest.mark.asyncio

    @pytest.fixture(autouse=True)
    def _no_real_pool(self, monkeypatch):
        self.conn = FakeConn()

        async def fake_get_pool():
            return FakePool(self.conn)

        monkeypatch.setattr(eval_store, "_get_pool", fake_get_pool)

    async def test_new_draft_inserts_eval_and_sections(self, ms_config):
        evaluation_id = await record_draft_evaluation(_scorecard(ms_config), ms_config)
        assert evaluation_id == 101
        eval_inserts = [i for i in self.conn.inserts if i[0] == "evaluations"]
        section_inserts = [i for i in self.conn.inserts if i[0] == "sections"]
        assert len(eval_inserts) == 1
        assert len(section_inserts) == 9
        assert all(args[0] == 101 for _, args in section_inserts)

    async def test_rescore_updates_row_and_replaces_sections(self, ms_config):
        self.conn.existing_id = 77
        evaluation_id = await record_draft_evaluation(_scorecard(ms_config), ms_config)
        assert evaluation_id == 77
        assert len(self.conn.updates) == 1
        assert self.conn.deletes == [(77,)]
        assert not [i for i in self.conn.inserts if i[0] == "evaluations"]

    async def test_db_failure_is_swallowed(self, ms_config, monkeypatch):
        async def exploding_pool():
            raise ConnectionError("pg down")

        monkeypatch.setattr(eval_store, "_get_pool", exploding_pool)
        assert await record_draft_evaluation(_scorecard(ms_config), ms_config) is None

    async def test_mid_write_failure_is_swallowed(self, ms_config, monkeypatch):
        async def boom(*a, **k):
            raise RuntimeError("constraint violation")

        self.conn.fetchval = boom
        assert await record_draft_evaluation(_scorecard(ms_config), ms_config) is None


class TestPoolDisabled:
    pytestmark = pytest.mark.asyncio

    async def test_no_database_url_returns_none(self, ms_config, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.setattr(eval_store, "_pool", None)
        assert await record_draft_evaluation(_scorecard(ms_config), ms_config) is None

"""R3 — qa.assessments persistence (JulyR2R3 §2).

Checkpoints from the design doc: a fresh generation persists (row +
sections + is_current flip), placeholders never persist, an unknown/
departed agent skips cleanly, invalid trends drop only their section
row, and get_progression's wiring persists exactly once per fresh
generation (cache hits and placeholders don't). All DB access faked.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

import pytest

import backend.services.assessment_store as astore
from backend.models.dashboard import ProgressionAssessment, SectionAssessment
from backend.services.score_compute import ActiveVersions
from tests.conftest import load_test_config


class FakeConn:
    def __init__(self, agent_id=16):
        self.agent_id = agent_id
        self.inserted_assessment = None
        self.section_inserts = []
        self.flips = []

    @asynccontextmanager
    async def transaction(self):
        yield

    async def fetchval(self, query, *args):
        if "FROM qa.agents" in query:
            return self.agent_id
        assert "INSERT INTO qa.assessments" in query
        self.inserted_assessment = args
        return 501

    async def execute(self, query, *args):
        if "SET is_current = FALSE" in query:
            self.flips.append(args)
        else:
            assert "INSERT INTO qa.assessment_sections" in query
            self.section_inserts.append(args)

    async def fetchrow(self, query, *args):  # pragma: no cover - unused here
        return None


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


def _result(**over) -> ProgressionAssessment:
    kw = dict(
        overall_assessment="Solid month with improving adherence.",
        section_assessments={},
        evaluation_count=13,
        time_range_days=30,
        data_source="PostgreSQL",
    )
    kw.update(over)
    return ProgressionAssessment(**kw)


@pytest.fixture
def store(monkeypatch):
    conn = FakeConn()

    async def fake_pool():
        return FakePool(conn)

    async def fake_versions(c, team_id):
        return ActiveVersions(formula_version="sales_v2", rubric_version="sales_v2r")

    monkeypatch.setattr(astore, "get_pool", fake_pool)
    import backend.services.score_compute as sc
    monkeypatch.setattr(sc, "get_active_versions", fake_versions)
    return conn


def test_fresh_generation_persists_row_flip_and_sections(store, monkeypatch):
    config = load_test_config("sales")
    sec = config.sections_by_number[0]
    result = _result(section_assessments={
        sec.history_id: SectionAssessment(
            trend="Improving", summary="s", coaching_tip="c"),
    })
    aid = asyncio.run(astore.persist_assessment(
        result, agent_name="Alexis López", config=config))
    assert aid == 501
    # row stamps
    args = store.inserted_assessment
    assert args[0] == 16                       # resolved agent_id
    assert args[1] == config.team_id
    assert args[2] == 30                       # time_range_days
    assert args[5] == 13                       # evaluations_included
    assert args[7] == "sales_v2r"              # rubric_version
    assert json.loads(args[9])["text"]["model"]  # models_used shape
    # predecessor flip scoped to (agent, team, window size), excluding self
    assert store.flips == [(16, config.team_id, 30, 501)]
    # section snapshot: short id + name + number, trend normalized
    (sargs,) = store.section_inserts
    assert sargs[1] == sec.id and sargs[2] == sec.name
    assert sargs[3] == sec.section_number
    assert sargs[4] == "improving"


def test_unknown_agent_skips_persist(store):
    store.agent_id = None
    config = load_test_config("sales")
    aid = asyncio.run(astore.persist_assessment(
        _result(), agent_name="Ghost Agent", config=config))
    assert aid is None
    assert store.inserted_assessment is None


def test_invalid_trend_drops_only_that_section(store):
    config = load_test_config("sales")
    s0, s1 = config.sections_by_number[0], config.sections_by_number[1]
    result = _result(section_assessments={
        s0.history_id: SectionAssessment(trend="skyrocketing", summary="s", coaching_tip="c"),
        s1.history_id: SectionAssessment(trend="stable", summary="s", coaching_tip="c"),
    })
    aid = asyncio.run(astore.persist_assessment(
        result, agent_name="A", config=config))
    assert aid == 501                          # assessment row still lands
    assert [s[1] for s in store.section_inserts] == [s1.id]


def test_no_active_versions_skips(store, monkeypatch):
    import backend.services.score_compute as sc

    async def boom(c, team_id):
        raise sc.VersionNotArchivedError("qa.rubric_versions", "x")

    monkeypatch.setattr(sc, "get_active_versions", boom)
    aid = asyncio.run(astore.persist_assessment(
        _result(), agent_name="A", config=load_test_config("sales")))
    assert aid is None
    assert store.inserted_assessment is None


def test_no_pool_skips(monkeypatch):
    async def no_pool():
        return None

    monkeypatch.setattr(astore, "get_pool", no_pool)
    aid = asyncio.run(astore.persist_assessment(
        _result(), agent_name="A", config=load_test_config("sales")))
    assert aid is None


# ---------------------------------------------------------------------------
# get_progression wiring — persists exactly once per FRESH generation
# ---------------------------------------------------------------------------

class _Provider:
    name = "PostgreSQL"

    def __init__(self, records):
        self._records = records

    async def get_agent_history(self, agent_name, days=30):
        return self._records


def _wire(monkeypatch, gemini_text):
    import backend.services.progression_service as ps
    calls = []

    async def fake_persist(result, *, agent_name, config, **kw):
        calls.append((agent_name, result.evaluation_count))
        return 1

    monkeypatch.setattr(ps, "_cache", {})
    import backend.services.assessment_store as ast
    monkeypatch.setattr(ast, "persist_assessment", fake_persist)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    # The Gemini client moved behind the llm/ seam (ModelProviderDesign
    # P1) — stub the async surface the GeminiTextProvider actually calls.
    class _Resp:
        text = gemini_text

    class _AioModels:
        async def generate_content(self, **kw):
            return _Resp()

    class _Aio:
        models = _AioModels()

    class _Client:
        def __init__(self, api_key=None):
            self.aio = _Aio()

    import backend.services.llm.gemini as llm_gemini
    monkeypatch.setattr(llm_gemini.genai, "Client", _Client)
    return ps, calls


def test_progression_persists_once_and_not_on_cache_hit(monkeypatch, sales):
    good = json.dumps({"overall_assessment": "fine", "section_assessments": []})
    ps, calls = _wire(monkeypatch, good)
    from backend.models.dashboard import EvaluationRecord
    rec = EvaluationRecord(
        timestamp="2026-07-01T00:00:00Z", agent_name="A", agent_email="",
        manager_email="", overall_score=90.0, sections={}, eval_id="E",
        dialpad_link=None,
    )
    provider = _Provider([rec])
    r1 = asyncio.run(ps.get_progression(provider, "A", days=30, config=sales))
    r2 = asyncio.run(ps.get_progression(provider, "A", days=30, config=sales))
    assert r1.evaluation_count == 1 and r2.evaluation_count == 1
    assert calls == [("A", 1)]                 # once — cache hit didn't re-persist


def test_progression_placeholders_never_persist(monkeypatch, sales):
    # no records → placeholder
    ps, calls = _wire(monkeypatch, "irrelevant")
    asyncio.run(ps.get_progression(_Provider([]), "A", days=30, config=sales))
    assert calls == []
    # parse failure → placeholder
    ps2, calls2 = _wire(monkeypatch, "NOT JSON {{{")
    from backend.models.dashboard import EvaluationRecord
    rec = EvaluationRecord(
        timestamp="2026-07-01T00:00:00Z", agent_name="B", agent_email="",
        manager_email="", overall_score=90.0, sections={}, eval_id="E",
        dialpad_link=None,
    )
    asyncio.run(ps2.get_progression(_Provider([rec]), "B", days=30, config=sales))
    assert calls2 == []

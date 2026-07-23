"""P2 — SOP retrieval policy (PulpoConnection §4.2): query build,
threshold, caching, block rendering with flag cautions, provenance
shape, mode gating, and never-raises failure semantics."""

from __future__ import annotations

import pytest

from backend.services.rag import factory
from backend.services.rag import sop_retrieval
from backend.services.rag.provider import RagDoc, RagFlag, RagHit, RagProvider
from backend.services.rag.sop_retrieval import (
    SopContext,
    build_sop_query,
    fetch_sop_context,
    pulpo_sop_mode,
    render_sop_block,
    score_threshold,
)


class FakeProvider(RagProvider):
    name = "fake"

    def __init__(self, hits, docs, fail=False):
        self._hits, self._docs, self._fail = hits, docs, fail
        self.search_calls = 0
        self.doc_calls = 0

    async def search(self, queries, *, limit=5):
        if self._fail:
            raise RuntimeError("provider exploded")
        self.search_calls += 1
        return [list(self._hits) for _ in queries]

    async def get_document(self, doc_id):
        self.doc_calls += 1
        return self._docs.get(doc_id)


def _hit(doc_id, score, flags=0):
    return RagHit(id=doc_id, title=f"T-{doc_id}", excerpt="…",
                  score=score, score_kind="cosine", flag_count=flags)


def _doc(doc_id, body="Policy body.", flags=()):
    return RagDoc(id=doc_id, title=f"T-{doc_id}", body=body,
                  flags=flags, updated_at="2026-07-01")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    sop_retrieval.reset_caches_for_tests()
    factory.reset_for_tests()
    monkeypatch.delenv("PULPO_SOP_MODE", raising=False)
    monkeypatch.delenv("PULPO_SCORE_THRESHOLD", raising=False)
    yield
    sop_retrieval.reset_caches_for_tests()
    factory.reset_for_tests()


def _install(monkeypatch, provider):
    monkeypatch.setattr(factory, "_provider", provider)
    monkeypatch.setattr(factory, "_resolved", True)


# -- pure helpers -----------------------------------------------------------


def test_query_prefers_disposition():
    assert build_sop_query("Access & Entry", "Smart-lock failure", "hello") == \
        "Access & Entry — Smart-lock failure"
    assert build_sop_query("Billing", None, "hello") == "Billing"


def test_query_falls_back_to_transcript_head():
    q = build_sop_query(None, None, "A" * 1000)
    assert q == "A" * 600


def test_query_none_without_material():
    assert build_sop_query(None, None, "   ") is None


def test_mode_gating(monkeypatch):
    assert pulpo_sop_mode() == "off"          # default: opt-in
    for value, expected in (("shadow", "shadow"), ("ON", "on"), ("bogus", "off")):
        monkeypatch.setenv("PULPO_SOP_MODE", value)
        assert pulpo_sop_mode() == expected


def test_threshold_env_override(monkeypatch):
    assert score_threshold() == 0.55
    monkeypatch.setenv("PULPO_SCORE_THRESHOLD", "0.7")
    assert score_threshold() == 0.7
    monkeypatch.setenv("PULPO_SCORE_THRESHOLD", "junk")
    assert score_threshold() == 0.55


def test_render_flag_caution():
    doc = _doc("d1", flags=(RagFlag(quote="reboot the lock", note="outdated"),))
    block = render_sop_block([(doc, _hit("d1", 0.8, flags=1))])
    assert "[SOP 1] T-d1" in block
    assert "under review" in block and "reboot the lock" in block


def test_render_caps_block_size():
    docs = [(_doc(f"d{i}", body="x" * 9000), _hit(f"d{i}", 0.9 - i * 0.1))
            for i in range(3)]
    block = render_sop_block(docs)
    assert len(block) <= sop_retrieval.BLOCK_CHAR_CAP + 100
    # Highest-scoring doc survives intact; the tail got truncated/dropped.
    assert "[SOP 1] T-d0" in block


# -- fetch_sop_context ------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_happy_path(monkeypatch):
    provider = FakeProvider(
        hits=[_hit("d1", 0.81), _hit("d2", 0.61), _hit("d3", 0.40)],
        docs={"d1": _doc("d1"), "d2": _doc("d2")},
    )
    _install(monkeypatch, provider)
    ctx = await fetch_sop_context(
        disposition_category="Access & Entry",
        disposition="Smart-lock failure",
        transcript_text="",
    )
    assert ctx.sop_title == "T-d1"
    assert "[SOP 1] T-d1" in ctx.block_text and "[SOP 2] T-d2" in ctx.block_text
    # d3 fell below τ=0.55 — never fetched.
    assert provider.doc_calls == 2
    assert [p["id"] for p in ctx.provenance] == ["d1", "d2"]
    assert ctx.provenance[0]["score"] == 0.81
    assert ctx.skipped_reason == ""


@pytest.mark.asyncio
async def test_fetch_below_threshold_is_conservative(monkeypatch):
    _install(monkeypatch, FakeProvider(hits=[_hit("d1", 0.30)], docs={}))
    ctx = await fetch_sop_context(
        disposition_category="Rare Category", disposition=None, transcript_text="",
    )
    assert ctx.block_text == "" and ctx.provenance == []
    assert ctx.skipped_reason == "below_threshold"


@pytest.mark.asyncio
async def test_fetch_caches_by_query(monkeypatch):
    provider = FakeProvider(hits=[_hit("d1", 0.9)], docs={"d1": _doc("d1")})
    _install(monkeypatch, provider)
    for _ in range(3):
        await fetch_sop_context(
            disposition_category="Billing", disposition="Refund",
            transcript_text="",
        )
    assert provider.search_calls == 1  # finite label space → cached
    assert provider.doc_calls == 1


@pytest.mark.asyncio
async def test_fetch_provider_error_never_raises(monkeypatch):
    _install(monkeypatch, FakeProvider(hits=[], docs={}, fail=True))
    ctx = await fetch_sop_context(
        disposition_category="Billing", disposition=None, transcript_text="",
    )
    assert isinstance(ctx, SopContext)
    assert ctx.skipped_reason == "provider_error"


@pytest.mark.asyncio
async def test_fetch_no_provider(monkeypatch):
    _install(monkeypatch, None)
    ctx = await fetch_sop_context(
        disposition_category="Billing", disposition=None, transcript_text="",
    )
    assert ctx.skipped_reason == "no_provider"


# -- eval-row provenance ----------------------------------------------------


def test_draft_row_carries_pulpo_docs():
    from backend.services.eval_store import build_draft_row
    from backend.models.scorecard import ScorecardWithMeta
    from tests.conftest import load_test_config

    config = load_test_config("member_support")
    provenance = [{"id": "d1", "title": "T-d1", "score": 0.81,
                   "score_kind": "cosine", "updated_at": "2026-07-01",
                   "open_flags": 0}]
    sc = ScorecardWithMeta(
        sections=[], key_strengths="", opportunities="", call_summary="",
        call_id="DP1", agent_name="Jane", pulpo_docs=provenance,
    )
    import json
    meta = json.loads(build_draft_row(sc, config)["dialpad_call_metadata"])
    assert meta["pulpo_docs"] == provenance

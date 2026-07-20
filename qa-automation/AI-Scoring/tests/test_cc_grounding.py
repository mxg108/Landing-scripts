"""C3 checkpoint — prompt-build tests for the grounding block.

DispositionDesign §5: disposition present / absent, holds / no-holds,
the v2.1 language rule (audio SOT for Spanish), block placement ahead of
the transcript, and the Stage-1 eval-row stamps.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from backend.models.scorecard import ScorecardWithMeta
from backend.prompts.qa_scoring_prompt import build_prompt
from backend.services.cc_context import (
    CallContext,
    HoldCycle,
    build_call_context_block,
    grounding_mode,
)
from backend.services.eval_store import build_draft_row
from tests.conftest import load_test_config

T0 = datetime(2026, 7, 15, 10, 0, 30, tzinfo=timezone.utc)


def _at(seconds: int) -> datetime:
    return datetime.fromtimestamp(T0.timestamp() + seconds, tz=timezone.utc)


def _ctx(**overrides) -> CallContext:
    defaults = dict(
        cc_call_id=1,
        matched_by="entry_point",
        disposition_category="Access & Entry",
        disposition="Smart-lock failure",
        ai_csat=4.5,
        total_hold_seconds=160,
        connected_at=T0,
        started_at=_at(-30),
        holds=[
            HoldCycle(_at(195), _at(297), 102),
            HoldCycle(_at(662), _at(720), 58),
        ],
    )
    defaults.update(overrides)
    return CallContext(**defaults)


@pytest.fixture(scope="module")
def config():
    return load_test_config("member_support")


# ---------------------------------------------------------------------------
# build_call_context_block — disposition present / absent
# ---------------------------------------------------------------------------


def test_disposition_present_scores_for_the_disposition():
    block = build_call_context_block(_ctx())
    assert "CALL CONTEXT (VERIFIED SYSTEM DATA)" in block
    assert "Access & Entry — Smart-lock failure" in block
    assert "FOR that disposition" in block


def test_bare_category_renders_without_sub():
    block = build_call_context_block(_ctx(disposition=None))
    assert "classified this call as: Access & Entry." in block


def test_absence_path_language_unknown_carries_both():
    """Stage-1 reality: language is detected in-flight, so the wording
    carries the transcript AND audio alternatives (v2.1 rule)."""
    block = build_call_context_block(
        _ctx(disposition_category=None, disposition=None)
    )
    assert "No disposition was captured" in block
    assert "transcript evidence alone" in block
    assert "audio content if the call is in Spanish" in block


def test_absence_path_spanish_is_audio_sot():
    """v2.1: for Spanish calls the AUDIO is the source of truth — no
    transcript-first phrasing anywhere in the sentence."""
    block = build_call_context_block(
        _ctx(disposition_category=None, disposition=None), language="es"
    )
    assert "AUDIO content" in block
    assert "audio is the source of truth" in block
    assert "transcript evidence alone" not in block


def test_absence_path_english_is_transcript_evidence():
    block = build_call_context_block(
        _ctx(disposition_category=None, disposition=None), language="en"
    )
    assert "transcript evidence alone" in block
    assert "AUDIO content" not in block


# ---------------------------------------------------------------------------
# build_call_context_block — holds / no-holds
# ---------------------------------------------------------------------------


def test_holds_render_duration_at_offset():
    """`1:42 at 3:15` — cycle duration at its offset from connect (§5)."""
    block = build_call_context_block(_ctx())
    assert "Verified hold record: 2 holds (1:42 at 3:15, 0:58 at 11:02)" in block
    assert "Do NOT infer" in block


def test_no_holds_kills_the_hallucinated_hold_class():
    block = build_call_context_block(_ctx(total_hold_seconds=0, holds=[]))
    assert "Verified: no holds occurred on this call" in block


def test_rollup_without_cycles_renders_total_only():
    """Backfill-era rows (stats_pull) can carry the rollup without
    per-cycle detail — state the total, never invent positions."""
    block = build_call_context_block(_ctx(holds=[], total_hold_seconds=160))
    assert "Verified total hold time: 2:40" in block
    assert "hold record:" not in block


def test_ai_csat_line_present_and_absent():
    assert "Ai CSAT estimate for this call: 4.5/5" in build_call_context_block(_ctx())
    assert "CSAT" not in build_call_context_block(_ctx(ai_csat=None))


def test_no_context_renders_nothing():
    """CC never saw the call → no block; the prompt stays as-is today."""
    assert build_call_context_block(None) == ""


def test_stats_row_never_claims_verified_no_holds():
    """A stats_pull-created row carries total_hold_seconds=0 by schema
    DEFAULT — that is NOT hold truth (the dispositions export has no
    hold data). The block must forbid fabrication without asserting
    absence."""
    block = build_call_context_block(
        _ctx(has_hold_truth=False, holds=[], total_hold_seconds=0)
    )
    assert "no holds occurred" not in block
    assert "No verified hold record is available" in block
    assert "Do NOT state specific hold counts" in block


def test_stats_row_ignores_any_hold_fields():
    """has_hold_truth=False wins even if hold fields are somehow set —
    the provenance flag is the gate, not the data."""
    block = build_call_context_block(_ctx(has_hold_truth=False))
    assert "Verified hold record" not in block
    assert "No verified hold record is available" in block


# ---------------------------------------------------------------------------
# build_prompt — placement ahead of the transcript
# ---------------------------------------------------------------------------


def test_context_block_rides_ahead_of_transcript(config):
    block = build_call_context_block(_ctx())
    p = build_prompt(
        config,
        transcript_text="Speaker A: hello",
        call_context_text=block,
    )
    assert p.index("CALL CONTEXT (VERIFIED SYSTEM DATA)") < p.index(
        "DIALPAD TRANSCRIPT"
    )


def test_empty_context_leaves_prompt_unchanged(config):
    with_none = build_prompt(config, transcript_text="Speaker A: hello")
    with_empty = build_prompt(
        config, transcript_text="Speaker A: hello", call_context_text=""
    )
    assert with_none == with_empty
    assert "CALL CONTEXT" not in with_empty


# ---------------------------------------------------------------------------
# Stage-1 eval-row stamps (§5 step 4)
# ---------------------------------------------------------------------------


def _scorecard(config, **overrides) -> ScorecardWithMeta:
    defaults = dict(
        sections=[],
        key_strengths="",
        opportunities="",
        call_summary="",
        call_id="DP123",
        agent_name="Jane Agent",
    )
    defaults.update(overrides)
    return ScorecardWithMeta(**defaults)


def test_draft_row_carries_cc_stamps(config):
    row = build_draft_row(
        _scorecard(
            config,
            dialpad_disposition_category="Access & Entry",
            dialpad_disposition="Smart-lock failure",
            ai_csat=4.5,
        ),
        config,
    )
    assert row["dialpad_disposition_category"] == "Access & Entry"
    assert row["dialpad_disposition"] == "Smart-lock failure"
    assert row["ai_csat"] == 4.5


def test_draft_row_stamps_null_when_unmatched(config):
    row = build_draft_row(_scorecard(config), config)
    assert row["dialpad_disposition_category"] is None
    assert row["dialpad_disposition"] is None
    assert row["ai_csat"] is None


# ---------------------------------------------------------------------------
# grounding_mode gate
# ---------------------------------------------------------------------------


def test_grounding_mode_default_is_shadow(monkeypatch):
    monkeypatch.delenv("CC_GROUNDING_MODE", raising=False)
    assert grounding_mode() == "shadow"


def test_grounding_mode_values(monkeypatch):
    for value, expected in (
        ("off", "off"), ("ON", "on"), (" shadow ", "shadow"),
        ("bogus", "shadow"),
    ):
        monkeypatch.setenv("CC_GROUNDING_MODE", value)
        assert grounding_mode() == expected

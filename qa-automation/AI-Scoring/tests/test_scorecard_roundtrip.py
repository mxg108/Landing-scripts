"""Scorecard model + sheets_service writer surface.

Phase 2 / Phase B (a) note: the four-stage pipeline replaces
``append_scorecard_row`` / ``update_scorecard_reasoning`` /
``write_approved_to_form_responses_1`` with stage functions that read
the new ``score_destination`` config. Until Phase B (a) lands, the
behavior-exercising tests here are skipped — only the schema-agnostic
scorecard validation and the source-string failure-point checks run.

Once Phase B (a) lands:
  * remove the skip markers and rewrite the behavior tests against the
    new stage functions
  * the source-string xfails should flip to passing (literals removed)
"""

from __future__ import annotations

import inspect

import pytest

from backend.config.team_config import TeamConfig
from backend.models.scorecard import ApprovalRequest, Scorecard, ScorecardSection
from backend.services import sheets_service
from pydantic import ValidationError

from tests.conftest import make_gemini_scoring_json


# ---------------------------------------------------------------------------
# Schema-agnostic — runs today
# ---------------------------------------------------------------------------

def test_synthetic_gemini_json_validates_via_scorecard(config: TeamConfig):
    raw = make_gemini_scoring_json(config)
    sc = Scorecard(**raw)
    expected_count = len(config.ai_scored_sections)
    assert len(sc.sections) == expected_count


# ---------------------------------------------------------------------------
# Behavior tests — pending Phase B (a)
# ---------------------------------------------------------------------------

@pytest.mark.skip(reason=(
    "pending Phase B (a) sheets_service refactor — append_scorecard_row "
    "still references removed schema fields (column_map, "
    "scored_section_columns). Will be replaced by Stage 1 writer that uses "
    "config.history_layout + score_destination."
))
def test_stage_1_writer_runs_against_derived_layout(config: TeamConfig, monkeypatch):
    """Placeholder for the new Stage 1 writer test (post-refactor).

    Should mock the FR-AI sheet, call the Stage 1 writer, and assert the
    captured row width matches ``config.history_layout.total_width``.
    """
    pass


# ---------------------------------------------------------------------------
# Failure points — source-string presence checks (schema-immune)
# ---------------------------------------------------------------------------

def test_sheets_service_does_not_hardcode_documentation_id():
    """FAILURE POINT #4 fixed in Phase B (a): manual-section handling
    iterates config.manual_sections / config.scoring_id_to_section
    instead of matching the literal 'documentation'. Guard against
    regression."""
    src = inspect.getsource(sheets_service)
    assert '"documentation"' not in src and "'documentation'" not in src, (
        "sheets_service re-introduced the literal 'documentation' section_id; "
        "Sales' manual sections (pb_creation, mc_call_notes) won't match"
    )


def test_sheets_service_does_not_hardcode_qai_range():
    """FAILURE POINT #5 fixed in Phase B (a): reasoning column placement
    is now derived via config.history_layout (no f-string Q:AI literal)."""
    src = inspect.getsource(sheets_service)
    # The old literal was f"Q{row_num}:AI{row_num}" — fingerprint }:AI{
    assert "}:AI{" not in src, (
        "sheets_service re-introduced the 'Q:AI' reasoning range; ranges "
        "must derive from history_layout (col_index_to_letter)"
    )


def test_sheets_service_does_not_hardcode_section_slicing():
    """FAILURE POINT #6 fixed in Phase B (a): destination column placement
    iterates score_destination.section_score_columns, not [:8] slicing."""
    src = inspect.getsource(sheets_service)
    assert "score_col_ids[:8]" not in src and "[:8]" not in src, (
        "sheets_service re-introduced [:8] section slicing — Stage 2 must "
        "iterate score_destination.section_score_columns"
    )


# ---------------------------------------------------------------------------
# ScorecardSection cross-field validator (always on)
# ---------------------------------------------------------------------------

def _base_section_kwargs(**overrides):
    base = dict(
        id="sec_x",
        name="Section X",
        score=None,
        score_type="numeric",
        yn_value=None,
        confidence="high",
        reasoning="ok",
    )
    base.update(overrides)
    return base


def test_section_validator_accepts_numeric_score():
    ScorecardSection(**_base_section_kwargs(score=4))


def test_section_validator_accepts_yn_value_with_yn_type():
    ScorecardSection(**_base_section_kwargs(score_type="yn", yn_value="Y"))


def test_section_validator_accepts_explicit_na_with_numeric_type():
    # numeric section, analyst/AI marked it N/A
    ScorecardSection(**_base_section_kwargs(score_type="numeric", yn_value="NA"))


def test_section_validator_rejects_na_with_numeric_score():
    with pytest.raises(ValidationError, match="requires score=None"):
        ScorecardSection(**_base_section_kwargs(yn_value="NA", score=3))


def test_section_validator_rejects_yn_value_on_numeric_section():
    with pytest.raises(ValidationError, match="only valid on yn"):
        ScorecardSection(**_base_section_kwargs(score_type="numeric", yn_value="Y"))


def test_section_validator_rejects_numeric_score_on_yn_section():
    with pytest.raises(ValidationError, match="only valid on numeric"):
        ScorecardSection(**_base_section_kwargs(score_type="yn", score=4))


def test_section_validator_rejects_score_and_yn_together():
    # Y/N section can't have a numeric score; numeric type with yn_value=Y also rejected.
    with pytest.raises(ValidationError):
        ScorecardSection(**_base_section_kwargs(score=4, yn_value="Y"))


# ---------------------------------------------------------------------------
# Context-aware validator — team config gates yn_value="NA"
# ---------------------------------------------------------------------------

def test_section_validator_rejects_na_when_team_config_disallows(sales: TeamConfig):
    """Defense-in-depth: future model providers may hallucinate NA on a section
    declared na_applicable=false. The context-aware validator rejects it."""
    sec_def = next(
        s for s in sales.ai_scored_sections if s.score_type == "numeric"
    )
    sec_def.na_applicable = False  # sales_v2 declares all sections NA-able
    data = _base_section_kwargs(id=sec_def.id, score_type="numeric", yn_value="NA")
    with pytest.raises(ValidationError, match="na_applicable=false"):
        ScorecardSection.model_validate(data, context={"section_def": sec_def})


def test_section_validator_allows_na_when_team_config_permits(sales: TeamConfig):
    sec_def = next(
        s for s in sales.ai_scored_sections if s.na_applicable
    )
    score_type = sec_def.score_type
    data = _base_section_kwargs(id=sec_def.id, score_type=score_type, yn_value="NA")
    # Should not raise.
    ScorecardSection.model_validate(data, context={"section_def": sec_def})


def test_section_validator_no_context_relaxed():
    """Without context, the team-config check is skipped — round-tripping stored
    data (where the team config isn't available) must still work."""
    ScorecardSection.model_validate(
        _base_section_kwargs(score_type="numeric", yn_value="NA")
    )


# ---------------------------------------------------------------------------
# _format_ai_score — NA wins regardless of score_type (Phase 2)
# ---------------------------------------------------------------------------

def _section_def(sales: TeamConfig, score_type: str, na_applicable: bool):
    """Find a section in sales matching score_type + na_applicable."""
    for s in sales.sections:
        if s.score_type == score_type and s.na_applicable is na_applicable:
            return s
    raise AssertionError(
        f"no section with score_type={score_type!r} and na_applicable={na_applicable}"
    )


def test_format_ai_score_numeric_with_na_renders_not_applicable(sales: TeamConfig):
    """Explicit yn_value='NA' on a numeric section must render 'Not Applicable'.

    This is the reported bug: numeric+na_applicable could not be marked N/A.
    The formatter now short-circuits on yn_value=='NA' before score_type."""
    sec_def = next(
        s for s in sales.ai_scored_sections if s.score_type == "numeric"
    )
    ai_section = {
        "id": sec_def.id,
        "score": None,
        "score_type": "numeric",
        "yn_value": "NA",
        "confidence": "high",
        "reasoning": "not applicable for this call",
    }
    assert sheets_service._format_ai_score(sec_def, ai_section) == "Not Applicable"


def test_format_ai_score_numeric_with_score_unchanged(sales: TeamConfig):
    """Regression: a normal numeric score still renders as '4' (string)."""
    sec_def = next(
        s for s in sales.ai_scored_sections if s.score_type == "numeric"
    )
    ai_section = {
        "id": sec_def.id,
        "score": 4,
        "score_type": "numeric",
        "yn_value": None,
        "confidence": "high",
        "reasoning": "solid",
    }
    assert sheets_service._format_ai_score(sec_def, ai_section) == "4"


def test_format_ai_score_unscored_numeric_renders_na_sentinel(sales: TeamConfig):
    """Regression: when AI didn't score (both None), curt 'N/A' (not 'Not
    Applicable') — preserves distinction from explicit analyst N/A."""
    sec_def = next(
        s for s in sales.ai_scored_sections if s.score_type == "numeric"
    )
    ai_section = {
        "id": sec_def.id,
        "score": None,
        "score_type": "numeric",
        "yn_value": None,
        "confidence": "low",
        "reasoning": "could not score",
    }
    assert sheets_service._format_ai_score(sec_def, ai_section) == "N/A"


def test_format_ai_score_yn_section_unchanged(sales: TeamConfig):
    """Regression: yn sections still map Y/N/NA through YN_DISPLAY."""
    sec_def = next(s for s in sales.ai_scored_sections if s.score_type == "yn")
    for yn, expected in [("Y", "Yes"), ("N", "No"), ("NA", "Not Applicable")]:
        ai_section = {
            "id": sec_def.id,
            "score": None,
            "score_type": "yn",
            "yn_value": yn,
            "confidence": "high",
            "reasoning": "ok",
        }
        assert sheets_service._format_ai_score(sec_def, ai_section) == expected


def test_scorecard_validates_with_sections_by_id_context(sales: TeamConfig):
    """Scorecard-level model_validate must propagate sections_by_id context to
    each ScorecardSection child, so the team-config check fires at the parse
    site used by the scoring pipeline."""
    bad_section = next(
        s for s in sales.ai_scored_sections if s.score_type == "numeric"
    )
    bad_section.na_applicable = False  # sales_v2 declares all sections NA-able
    bad_section_id = bad_section.id
    raw = {
        "sections": [
            {
                "id": bad_section_id,
                "name": "x",
                "score": None,
                "score_type": "numeric",
                "yn_value": "NA",
                "confidence": "high",
                "reasoning": "model hallucinated NA",
            }
        ],
        "key_strengths": "k",
        "opportunities": "o",
    }
    sections_by_id = {s.id: s for s in sales.sections}
    with pytest.raises(ValidationError, match="na_applicable=false"):
        Scorecard.model_validate(raw, context={"sections_by_id": sections_by_id})


def test_approval_request_with_context_rejects_bad_na(sales: TeamConfig):
    """ApprovalRequest mirrors Scorecard's section validation; the route
    re-validates with context to enforce team-config compliance. Today no
    production team has a yn+na_applicable=false section, but a future team
    might — flip a yn section's na_applicable to exercise the path."""
    bad = next(s for s in sales.ai_scored_sections if s.score_type == "yn")
    bad.na_applicable = False
    try:
        raw = {
            "sections": [
                {
                    "id": bad.id,
                    "name": bad.name,
                    "score": None,
                    "score_type": "yn",
                    "yn_value": "NA",
                    "confidence": "high",
                    "reasoning": "tampered payload",
                }
            ],
            "key_strengths": "",
            "opportunities": "",
        }
        sections_by_id = {s.id: s for s in sales.sections}
        with pytest.raises(ValidationError, match="na_applicable=false"):
            ApprovalRequest.model_validate(raw, context={"sections_by_id": sections_by_id})
    finally:
        bad.na_applicable = True


def test_scorecard_section_model_dump_round_trips_through_formatter(sales: TeamConfig):
    """The full path used by Stage 1: ScorecardSection -> .model_dump() ->
    _format_ai_score. An explicit numeric N/A must survive the round trip."""
    sec_def = next(
        s for s in sales.ai_scored_sections if s.score_type == "numeric"
    )
    section = ScorecardSection(
        id=sec_def.id,
        name=sec_def.name,
        score=None,
        score_type="numeric",
        yn_value="NA",
        confidence="high",
        reasoning="not applicable",
    )
    assert sheets_service._format_ai_score(sec_def, section.model_dump()) == "Not Applicable"

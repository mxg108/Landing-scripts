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
from backend.models.scorecard import Scorecard
from backend.services import sheets_service

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

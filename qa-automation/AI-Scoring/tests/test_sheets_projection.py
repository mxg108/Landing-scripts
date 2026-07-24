"""DB → Sheets projection tests — cutover slice 2 (CutoverDesign §3a).

The row builder is pure: a qa.evaluations record + section rows in, a
HistoryLayout-shaped list of cell strings out. These pin the rendering
contract the GAS email pipeline and dashboards read.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from backend.config import history_layout
from backend.services.sheets_projection import _render_overall, build_projection_row
from tests.conftest import load_test_config


@pytest.fixture(scope="module")
def ms_config():
    return load_test_config("member_support")


def _evaluation(**overrides):
    base = {
        "agent_name_raw": "Jane Agent",
        "agent_email": "jane@landing.com",
        "call_connected_at": datetime(2026, 7, 4, 15, 30, tzinfo=timezone.utc),
        "evaluator_email": "lead@landing.com",
        "dialpad_link": "https://dialpad.test/call/DP123",
        "overall_score": 87.5,
        "key_strengths": "Good tone",
        "opportunities": "Faster holds",
        "call_summary": "Billing question",
        "caller_name": "Pat Caller",
        "caller_phone": "+15550100",
        "source": "ai",
        "approved_at": datetime(2026, 7, 4, 16, 0, tzinfo=timezone.utc),
    }
    base.update(overrides)
    return base


def _sections():
    def row(sid, numeric=None, binary=None, confidence="HIGH", reasoning="because"):
        return {"section_id": sid, "numeric_score": numeric, "binary_value": binary,
                "confidence": confidence, "reasoning": reasoning}
    return [
        row("greeting", numeric=5),
        row("caller_id", binary="Y", confidence="MED"),
        row("purpose", numeric=4),
        row("matching", numeric=4),
        row("process_adherence", numeric=5),
        row("call_resolution", numeric=3),
        row("comms", numeric=4),
        row("efficiency", numeric=2),
        row("human_review_required", binary="NA", confidence=None, reasoning=None),
        row("cri", binary="Y", confidence=None, reasoning=None),
    ]


class TestBuildProjectionRow:

    def test_prefix_and_trailing_columns(self, ms_config):
        row = build_projection_row(_evaluation(), _sections(), ms_config)
        L = ms_config.history_layout
        assert len(row) == L.total_width
        assert row[history_layout.COL_AGENT_NAME] == "Jane Agent"
        assert row[history_layout.COL_AGENT_EMAIL] == "jane@landing.com"
        assert row[history_layout.COL_TIMESTAMP] == "07/04/2026 15:30:00"
        assert row[history_layout.COL_EVALUATOR_EMAIL] == "lead@landing.com"
        assert row[history_layout.COL_DIALPAD_LINK] == "https://dialpad.test/call/DP123"
        assert row[history_layout.COL_OVERALL_SCORE] == "87.5"
        assert row[L.col_key_strengths] == "Good tone"
        assert row[L.col_source] == "ai"
        assert row[L.col_eval_approved_at] == "07/04/2026 16:00:00"

    def test_section_cells_mirror_legacy_writer(self, ms_config):
        """Numeric scores as digits; binary via YN_DISPLAY; NA as
        'Not Applicable'; confidence lowercased like the Stage-1 writer."""
        row = build_projection_row(_evaluation(), _sections(), ms_config)
        L = ms_config.history_layout
        by_number = {s.id: i for i, s in enumerate(ms_config.sections_by_number)}
        assert row[L.col_score(by_number["greeting"])] == "5"
        assert row[L.col_score(by_number["caller_id"])] == "Yes"
        assert row[L.col_score(by_number["human_review_required"])] == "Not Applicable"
        assert row[L.col_confidence(by_number["caller_id"])] == "med"
        assert row[L.col_reasoning(by_number["greeting"])] == "because"

    def test_missing_section_row_leaves_cells_blank(self, ms_config):
        sections = [s for s in _sections() if s["section_id"] != "cri"]
        row = build_projection_row(_evaluation(), sections, ms_config)
        L = ms_config.history_layout
        idx = next(i for i, s in enumerate(ms_config.sections_by_number) if s.id == "cri")
        assert row[L.col_score(idx)] == ""

    def test_null_fields_render_empty(self, ms_config):
        evaluation = _evaluation(
            agent_email=None, overall_score=None, approved_at=None,
            call_connected_at=None, caller_name=None,
        )
        row = build_projection_row(evaluation, _sections(), ms_config)
        L = ms_config.history_layout
        assert row[history_layout.COL_AGENT_EMAIL] == ""
        assert row[history_layout.COL_OVERALL_SCORE] == ""
        assert row[history_layout.COL_TIMESTAMP] == ""
        assert row[L.col_eval_approved_at] == ""

    def test_call_metadata_trailing_columns(self, ms_config):
        """Disposition/CSAT/SOP-reference cells the GAS email reads —
        pulpo_docs footnote numbering must match the [SOP n] citations
        in the reasoning text (injection order preserved)."""
        evaluation = _evaluation(
            dialpad_disposition_category="Unit Issues",
            dialpad_disposition="Lockouts",
            ai_csat=4.5,
            dialpad_call_metadata=(
                '{"sop_used": "Lockout SOP", "pulpo_docs": ['
                '{"id": "a", "title": "Lockout SOP", "score": 0.91},'
                '{"id": "b", "title": "Latch Troubleshooting", "score": 0.72}]}'
            ),
        )
        row = build_projection_row(evaluation, _sections(), ms_config)
        L = ms_config.history_layout
        assert row[L.col_disposition] == "Unit Issues — Lockouts"
        assert row[L.col_ai_csat] == "4.5"
        assert row[L.col_sop_references] == (
            "SOP 1: Lockout SOP\nSOP 2: Latch Troubleshooting"
        )

    def test_call_metadata_absent_renders_blank(self, ms_config):
        """Fixture without the metadata keys (and rows scored before the
        CC/Pulpo era) render blank cells, never raise."""
        row = build_projection_row(_evaluation(), _sections(), ms_config)
        L = ms_config.history_layout
        assert row[L.col_disposition] == ""
        assert row[L.col_ai_csat] == ""
        assert row[L.col_sop_references] == ""

    def test_sop_references_falls_back_to_sop_used(self, ms_config):
        """Pre-provenance rows carry only the bare title."""
        evaluation = _evaluation(
            dialpad_call_metadata='{"sop_used": "Lockout SOP", "pulpo_docs": []}',
        )
        row = build_projection_row(evaluation, _sections(), ms_config)
        assert row[ms_config.history_layout.col_sop_references] == "Lockout SOP"


class TestOverallRendering:

    @pytest.mark.parametrize("value,expected", [
        (87.5, "87.5"),
        (87.0, "87"),      # clean integers keep the legacy look
        (100.0, "100"),
        (46.9, "46.9"),
        (None, ""),
    ])
    def test_render_overall(self, value, expected):
        assert _render_overall(value) == expected

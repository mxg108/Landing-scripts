"""R3b — one-pager DB swap (JulyR2R3 §3).

month_bounds bucket-TZ math, the frame→render adapter, the assessment
slot HTML, and get_or_generate_month_assessment's fetch-first /
no-generate flows (DB + Gemini faked).
"""

from __future__ import annotations

import asyncio
import importlib.util
from datetime import datetime, timezone
from pathlib import Path

import backend.services.assessment_store as astore
from backend.services.team_stats import load_and_clean
from tests.conftest import make_history_sheet, make_mails_sheet, load_test_config

_spec = importlib.util.spec_from_file_location(
    "export_onepager",
    Path(__file__).resolve().parent.parent / "scripts" / "export_onepager.py")
onepager = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(onepager)


# ---------------------------------------------------------------------------
# month_bounds — the TZ seam
# ---------------------------------------------------------------------------

def test_month_bounds_bucket_tz():
    start, end, days = astore.month_bounds("2026-06")
    assert days == 30
    # June 1 00:00 in America/Los_Angeles (PDT, UTC-7) = 07:00 UTC
    assert start == datetime(2026, 6, 1, 7, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 7, 1, 7, 0, tzinfo=timezone.utc)


def test_month_bounds_december_rollover():
    start, end, days = astore.month_bounds("2026-12")
    assert days == 31
    assert end.year == 2027 and end.month == 1


# ---------------------------------------------------------------------------
# frame → render adapter
# ---------------------------------------------------------------------------

def _month_frame(sales):
    df = load_and_clean(
        make_history_sheet(sales),
        make_mails_sheet(["Star Rep", "Decline Rep", "Improve Rep",
                          "Steady Rep", "Junior Rep"]),
        sales)
    return df


def test_frame_to_render_shape(sales):
    df = _month_frame(sales)
    rdf, section_cols = onepager.frame_to_render(df, "Star Rep", sales)
    assert not rdf.empty
    assert {"Agent Name", "_ts", "Overall Score"} <= set(rdf.columns)
    # canonical order, display labels
    expected = [s.name for s in sales.sections_by_number
                if s.history_id in df.columns]
    assert section_cols == expected
    # render end-to-end on the adapted frame
    html_out = onepager.render(rdf, "Star Rep", "2026-04", section_cols)
    assert "Star Rep" in html_out and "qa.evaluations" in html_out


# ---------------------------------------------------------------------------
# assessment slot
# ---------------------------------------------------------------------------

def test_assessment_html_placeholder_and_content():
    assert "No persisted assessment" in onepager.assessment_html(None)
    filled = onepager.assessment_html({
        "overall_assessment": "Strong month overall.",
        "evaluations_included": 12,
        "rubric_version": "ms_v2r",
        "generated_at": datetime(2026, 7, 1, tzinfo=timezone.utc),
        "sections": [
            {"section_name": "Greeting", "trend": "improving",
             "coaching_tip": "Keep the warm open."},
            {"section_name": "Efficiency", "trend": "declining",
             "coaching_tip": "Tighten wrap-up."},
        ],
    })
    assert "Strong month overall." in filled
    assert "Greeting" in filled and "▲" in filled and "▼" in filled
    assert "ms_v2r" in filled


# ---------------------------------------------------------------------------
# get_or_generate flow
# ---------------------------------------------------------------------------

def test_get_or_generate_prefers_existing_row(monkeypatch):
    config = load_test_config("sales")
    existing = {"overall_assessment": "cached row", "sections": []}
    calls = {"generated": 0}

    async def fake_fetch(cfg, agent, start, end):
        return existing

    monkeypatch.setattr(astore, "fetch_assessment_for_range", fake_fetch)

    def boom(*a, **k):
        calls["generated"] += 1
        raise AssertionError("must not generate when a row exists")

    import backend.services.progression_service as ps
    monkeypatch.setattr(ps, "generate_from_records", boom)
    got = asyncio.run(astore.get_or_generate_month_assessment(
        config, "A", "2026-06"))
    assert got is existing
    assert calls["generated"] == 0


def test_get_or_generate_no_generate_returns_none(monkeypatch):
    config = load_test_config("sales")

    async def fake_fetch(cfg, agent, start, end):
        return None

    monkeypatch.setattr(astore, "fetch_assessment_for_range", fake_fetch)
    got = asyncio.run(astore.get_or_generate_month_assessment(
        config, "A", "2026-06", generate=False))
    assert got is None

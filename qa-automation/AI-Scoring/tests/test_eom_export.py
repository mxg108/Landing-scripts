"""R2 — EOM CSV export summarization (JulyR2R3 §1).

Pure-helper tests on a synthetic load_and_clean frame (the exact schema
fetch_history_frame emits): month filtering matches the bucket-TZ
drill-down semantics, the sampling counter counts per agent, active-lens
default, summary/detail column shapes, and the slug.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from backend.services.team_stats import load_and_clean
from tests.conftest import make_history_sheet, make_mails_sheet

_spec = importlib.util.spec_from_file_location(
    "export_eom_csvs",
    Path(__file__).resolve().parent.parent / "scripts" / "export_eom_csvs.py")
eom = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(eom)


def _frame(sales):
    history = make_history_sheet(sales)
    mails = make_mails_sheet(
        ["Star Rep", "Decline Rep", "Improve Rep", "Steady Rep", "Junior Rep"])
    return load_and_clean(history, mails, sales)


def test_month_frame_filters_month_and_active(sales):
    df = _frame(sales)
    dm = eom.month_frame(df, "2026-04", include_departed=False)
    assert not dm.empty
    assert dm["is_active"].all()
    # Test Agent (not in Mails) is inactive → excluded by the active lens
    assert "Test Agent" not in set(dm["agent"])
    # widened lens brings the departed row back
    wide = eom.month_frame(df, "2026-04", include_departed=True)
    assert "Test Agent" in set(wide["agent"])
    # a month with no data is empty, not an error
    assert eom.month_frame(df, "1999-01", include_departed=False).empty


def test_team_summary_counts_and_target(sales):
    dm = eom.month_frame(_frame(sales), "2026-04", include_departed=False)
    summary = eom.team_summary(dm, sales, target=20)
    assert set(summary["agent"]) == set(dm["agent"].unique())
    star = summary[summary["agent"] == "Star Rep"].iloc[0]
    n_star = int((dm["agent"] == "Star Rep").sum())
    assert star["evals"] == n_star                       # the sampling counter
    assert star["sampling_target"] == 20
    assert star["pct_of_target"] == round(100.0 * n_star / 20, 1)
    # rollup sanity + roster convention (sorted by mean desc)
    assert summary.iloc[0]["mean"] == summary["mean"].max()
    # per-section columns present (labels, not history_ids)
    first_label = list(sales.section_labels.values())[0]
    assert first_label in summary.columns


def test_agent_detail_rows_and_link(sales):
    dm = eom.month_frame(_frame(sales), "2026-04", include_departed=False)
    detail = eom.agent_detail(dm, "Star Rep", sales)
    assert len(detail) == int((dm["agent"] == "Star Rep").sum())
    assert list(detail.columns)[0] == "call_date"
    assert detail["dialpad_link"].str.startswith(
        "https://dialpad.com/callhistory/callreview/").all()
    # call-date ordered
    assert list(detail["call_date"]) == sorted(detail["call_date"])


def test_agent_slug_accent_folding():
    assert eom.agent_slug("Alexis López") == "alexis_lopez"
    assert eom.agent_slug("  Fernanda   Santillán ") == "fernanda_santillan"

"""F4 — read-path shadow comparator + mode-aware /team frame helper.

Two layers (ReadPathFlip §5 F4):
- read_path_shadow.align/compare: key alignment, membership classification
  (sheet-only / db-only / common are deltas, not failures), and common-row
  cell diffing — the SAME comparator the offline harness uses.
- routes.team._team_history_frame: QA_READ_PATH dispatch — postgres serves
  the row-source (no provider._ws), shadow serves the sheet AND logs the
  delta, sheets serves the sheet with no shadow work.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import pandas as pd
import pytest

from backend.services import read_path_shadow as shadow


def _frame(rows):
    """rows: list of (eval_id, agent, day, overall)."""
    return pd.DataFrame([
        {"eval_id": e, "agent": a,
         "timestamp": datetime(2026, 4, d, 9, 0, 0), "overall_score": float(o)}
        for e, a, d, o in rows
    ])


# ---------------------------------------------------------------------------
# Comparator
# ---------------------------------------------------------------------------

def test_membership_classification():
    sheet = _frame([("A", "Star Rep", 1, 90), ("B", "Star Rep", 2, 80),
                    ("C", "Decline Rep", 3, 70)])
    pg = _frame([("B", "Star Rep", 2, 80), ("C", "Decline Rep", 3, 70),
                 ("D", "Improve Rep", 4, 60)])
    m = shadow.compare(sheet, pg)["membership"]
    assert m["common"] == 2
    assert m["sheet_only"] == 1  # A — a B0 exclusion analogue
    assert m["db_only"] == 1     # D — backfilled-history analogue


def test_identical_common_rows_zero_cell_diffs():
    sheet = _frame([("A", "Star Rep", 1, 90), ("B", "Decline Rep", 2, 80)])
    result = shadow.compare(sheet, sheet.copy())
    assert result["cell_diff_count"] == 0


def test_cell_diff_detected_on_common_row():
    sheet = _frame([("A", "Star Rep", 1, 90), ("B", "Decline Rep", 2, 80)])
    pg = sheet.copy()
    pg.loc[pg["eval_id"] == "B", "overall_score"] = 81.0
    result = shadow.compare(sheet, pg)
    assert result["cell_diff_count"] == 1
    assert result["cell_diff_sample"][0]["col"] == "overall_score"
    # a score divergence is genuinely unexplained → 'other'
    assert result["classified"]["other"] == 1


def test_classify_clock_and_roster_deltas():
    """clock (±seconds on a timestamp col) and roster (is_active/supervisor)
    are KNOWN source-of-truth deltas, not 'other' failures."""
    sheet = pd.DataFrame([{
        "eval_id": "A", "agent": "Star Rep",
        "timestamp": datetime(2026, 4, 1, 9, 0, 0), "overall_score": 90.0,
        "is_active": True, "supervisor": "Max",
    }])
    pg = sheet.copy()
    pg.loc[0, "timestamp"] = datetime(2026, 4, 1, 9, 0, 1)   # B2 clock repair
    pg.loc[0, "is_active"] = False                            # stale roster
    pg.loc[0, "supervisor"] = ""
    cls = shadow.compare(sheet, pg)["classified"]
    assert cls["clock"] == 1
    assert cls["roster"] == 2
    assert cls["other"] == 0


def test_accent_folded_key_aligns_then_surfaces_name_drift():
    """The key folds accents so a roster-spelling drift still ALIGNS the
    row (common, not an opaque sheet-only+db-only pair) — and then the
    verbatim agent cell surfaces the drift as an informative diff. That
    matters: compute_agent_roster groups on the raw agent string, so
    differing spellings are a real analytics divergence, not cosmetic."""
    sheet = _frame([("A", "José Peña", 1, 90)])
    pg = _frame([("A", "Jose Pena", 1, 90)])
    result = shadow.compare(sheet, pg)
    assert result["membership"]["common"] == 1          # aligned, not split
    assert result["membership"]["sheet_only"] == 0
    assert result["cell_diff_count"] == 1               # drift surfaced
    assert result["cell_diff_sample"][0]["col"] == "agent"
    # classified as name_accent (canonicalization), NOT an unexplained fail
    assert result["classified"]["name_accent"] == 1
    assert result["classified"]["other"] == 0


def test_log_shadow_never_raises_on_empty():
    # empty pg frame must not blow up the request path
    shadow.log_shadow("sales", _frame([("A", "Star Rep", 1, 90)]), pd.DataFrame())


# ---------------------------------------------------------------------------
# Mode-aware /team frame helper
# ---------------------------------------------------------------------------

class _FakeSheetsProvider:
    def __init__(self):
        self._ws = self
    def get_all_values(self):
        return [["header"]]
    def _get_mails_sheet(self):
        return [["Agent Name"]]


@pytest.fixture
def _patch_team(monkeypatch):
    import backend.routes.team as team

    sheet_df = _frame([("S", "Star Rep", 1, 90)])
    pg_df = _frame([("P", "Star Rep", 1, 90)])
    calls = {"log_shadow": [], "fetch": 0, "get_provider": 0}

    async def _fake_fetch(config):
        calls["fetch"] += 1
        return pg_df

    async def _fake_get_provider(team_id):
        calls["get_provider"] += 1
        return _FakeSheetsProvider()

    monkeypatch.setattr(team, "fetch_history_frame", _fake_fetch)
    monkeypatch.setattr(team, "get_provider", _fake_get_provider)
    monkeypatch.setattr(team, "load_and_clean", lambda *a, **k: sheet_df)
    monkeypatch.setattr(team, "log_shadow",
                        lambda tid, s, p: calls["log_shadow"].append((tid, len(s), len(p))))
    monkeypatch.delenv("QA_READ_PATH", raising=False)
    return team, sheet_df, pg_df, calls


def test_helper_postgres_serves_rowsource(_patch_team, monkeypatch):
    team, sheet_df, pg_df, calls = _patch_team
    monkeypatch.setenv("QA_READ_PATH", "postgres")
    df = asyncio.run(team._team_history_frame("sales", config=None))
    assert df is pg_df
    assert calls["fetch"] == 1
    assert calls["get_provider"] == 0  # no _ws access on the postgres path
    assert calls["log_shadow"] == []


def test_helper_sheets_serves_sheet_no_shadow(_patch_team, monkeypatch):
    team, sheet_df, pg_df, calls = _patch_team
    monkeypatch.setenv("QA_READ_PATH", "sheets")
    df = asyncio.run(team._team_history_frame("sales", config=None))
    assert df is sheet_df
    assert calls["fetch"] == 0
    assert calls["log_shadow"] == []


def test_helper_shadow_serves_sheet_and_logs(_patch_team, monkeypatch):
    team, sheet_df, pg_df, calls = _patch_team
    monkeypatch.setenv("QA_READ_PATH", "shadow")
    df = asyncio.run(team._team_history_frame("sales", config=None))
    assert df is sheet_df           # sheet is still the served source of truth
    assert calls["fetch"] == 1      # but the pg frame was computed
    assert calls["log_shadow"] == [("sales", 1, 1)]  # and the delta logged


def test_helper_shadow_survives_pg_failure(_patch_team, monkeypatch):
    team, sheet_df, pg_df, calls = _patch_team

    async def _boom(config):
        raise RuntimeError("db down")

    monkeypatch.setattr(team, "fetch_history_frame", _boom)
    monkeypatch.setenv("QA_READ_PATH", "shadow")
    df = asyncio.run(team._team_history_frame("sales", config=None))
    assert df is sheet_df           # request still served despite pg failure
    assert calls["log_shadow"] == []

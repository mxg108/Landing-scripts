"""Parity comparator tests (read_path_shadow).

read_path_shadow.align/compare: key alignment, membership classification
(sheet-only / db-only / common are deltas, not failures), and common-row
cell diffing — the comparator the golden-parity harness layers compute_*
and endpoint permutations on. The F4 shadow-dispatch tests were deleted
with the shadow path in F5.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd

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


def test_key_ordinal_robust_to_clock_jitter():
    """A same-eval_id D2 pair must align by overall_score, not timestamp:
    the two sources' clocks differ by ~a second (B2 repair), so a
    timestamp-first ordinal would flip the pair's order and cross-pair
    their rows — the observed MS 'swapped section scores' artifact."""
    sheet = pd.DataFrame([
        {"eval_id": "X", "agent": "A", "overall_score": 95.0,
         "timestamp": datetime(2026, 1, 1, 0, 0, 0)},
        {"eval_id": "X", "agent": "A", "overall_score": 80.0,
         "timestamp": datetime(2026, 1, 1, 0, 0, 1)},
    ])
    pg = pd.DataFrame([   # same evals, clocks repaired in opposite directions
        {"eval_id": "X", "agent": "A", "overall_score": 95.0,
         "timestamp": datetime(2026, 1, 1, 0, 0, 2)},
        {"eval_id": "X", "agent": "A", "overall_score": 80.0,
         "timestamp": datetime(2026, 1, 1, 0, 0, 0)},
    ])
    # both 95-overall rows (row 0 in each) must land on the same key
    assert shadow.key_series(sheet)[0] == shadow.key_series(pg)[0]


def test_key_ordinal_pairs_by_content_when_fully_tied():
    """The prod-observed case the overall_score tiebreak missed: a D2 pair
    tied on eval_id AND overall_score AND second-truncated timestamp, with
    section values in opposite row order across sources (sub-second clock
    jitter decided the old ordinal). Content ordering must pair (5,5) with
    (5,5) — zero diffs."""
    def rows(order):
        return pd.DataFrame([
            {"eval_id": "X", "agent": "A", "overall_score": 90.0,
             "timestamp": datetime(2026, 1, 16, 20, 49, 40, us),
             "matching_the_moment": v}
            for us, v in order
        ])
    sheet = rows([(0, 5.0), (500000, 4.0)])
    pg = rows([(600000, 4.0), (100000, 5.0)])   # opposite order, jittered µs
    result = shadow.compare(sheet, pg)
    assert result["membership"]["common"] == 2
    assert result["cell_diff_count"] == 0


def test_key_content_pairing_cannot_mask_real_divergence():
    """Content ordering must NOT hide a genuine value difference inside a
    same-key pair: sheet (5, 4) vs pg (5, 3) still shows exactly one diff."""
    def rows(vals):
        return pd.DataFrame([
            {"eval_id": "X", "agent": "A", "overall_score": 90.0,
             "timestamp": datetime(2026, 1, 16, 20, 49, 40),
             "matching_the_moment": v}
            for v in vals
        ])
    result = shadow.compare(rows([5.0, 4.0]), rows([3.0, 5.0]))
    assert result["cell_diff_count"] == 1
    d = result["cell_diff_sample"][0]
    assert d["col"] == "matching_the_moment"
    assert (d["sheet"], d["pg"]) == (4.0, 3.0)   # 5s paired; 4 vs 3 surfaces

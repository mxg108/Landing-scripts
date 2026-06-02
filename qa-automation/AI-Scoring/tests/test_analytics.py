"""End-to-end test of team_stats functions against synthetic Sheets data.

Phase 2 / Phase B (b) complete: ``load_and_clean`` and the ``compute_*``
functions consume ``config.history_layout``. Skip markers removed; the
two former failure-point xfails (#7 priority-threshold scaling, #8
``len(row) < 15`` literal) are now regular passing tests guarding
against regression.
"""

from __future__ import annotations

import pytest

from backend.config.team_config import TeamConfig
from backend.services.team_stats import (
    _parse_yn_cell,
    compute_agent_roster,
    compute_binary_stats,
    compute_distribution,
    compute_ewma,
    compute_long_form,
    compute_monthly_spc,
    compute_monthly_summary,
    compute_outliers,
    compute_section_analysis,
    compute_supervisor_stats,
    load_and_clean,
)

from tests.conftest import make_history_row, make_history_sheet, make_mails_sheet


# ---------------------------------------------------------------------------
# Things that work today
# ---------------------------------------------------------------------------

def test_load_and_clean_produces_dataframe(config: TeamConfig):
    history = make_history_sheet(config)
    mails = make_mails_sheet([
        "Star Rep", "Decline Rep", "Improve Rep", "Steady Rep", "Junior Rep"
    ])
    df = load_and_clean(history, mails, config)
    assert not df.empty
    agents = set(df["agent"].unique())
    assert {"Star Rep", "Decline Rep", "Improve Rep", "Steady Rep", "Junior Rep"} <= agents


def test_excluded_test_agents_are_filtered_when_listed(sales: TeamConfig):
    """Real Sales config excludes 'Maximiliano Perez'."""
    history = make_history_sheet(sales)
    mails = make_mails_sheet(["Star Rep", "Maximiliano Perez"])
    df = load_and_clean(history, mails, sales)
    assert "Maximiliano Perez" not in set(df["agent"].unique())


def test_compute_long_form_sections_match_config(config: TeamConfig):
    history = make_history_sheet(config)
    mails = make_mails_sheet(["Star Rep", "Decline Rep", "Improve Rep", "Steady Rep", "Junior Rep"])
    df = load_and_clean(history, mails, config)
    long_df = compute_long_form(df, config)
    assert not long_df.empty
    seen = set(long_df["section_id"].unique())
    expected = set(config.numeric_history_ids) | set(config.yn_history_ids)
    assert seen == expected


def test_outlier_detection_finds_intentional_outlier(config: TeamConfig):
    history = make_history_sheet(config)
    mails = make_mails_sheet(["Star Rep", "Decline Rep", "Improve Rep", "Steady Rep", "Junior Rep"])
    df = load_and_clean(history, mails, config)
    outliers = compute_outliers(df, config.stats)
    star_outliers = [o for o in outliers if o["agent"] == "Star Rep"]
    assert star_outliers, (
        "Star Rep's intentional 60-overall call should register as a "
        "concerning outlier"
    )
    assert any(o["classification"] == "concerning" for o in star_outliers)


def test_ewma_detects_declining_and_improving_trends(config: TeamConfig):
    history = make_history_sheet(config)
    mails = make_mails_sheet(["Star Rep", "Decline Rep", "Improve Rep", "Steady Rep", "Junior Rep"])
    df = load_and_clean(history, mails, config)
    ewma = compute_ewma(df, config.stats)
    by_agent = {e["agent"]: e for e in ewma}
    assert by_agent["Decline Rep"]["trend"] == "declining"
    assert by_agent["Improve Rep"]["trend"] == "improving"


def test_binary_stats_returns_entry_per_yn_section(config: TeamConfig):
    history = make_history_sheet(config)
    mails = make_mails_sheet(["Star Rep", "Decline Rep", "Improve Rep", "Steady Rep", "Junior Rep"])
    df = load_and_clean(history, mails, config)
    binary = compute_binary_stats(df, config.yn_section_labels)
    assert len(binary) == len(config.yn_history_ids)
    seen_ids = {b["section_id"] for b in binary}
    assert seen_ids == set(config.yn_history_ids)


def test_all_compute_functions_run_without_error(config: TeamConfig):
    history = make_history_sheet(config)
    mails = make_mails_sheet(["Star Rep", "Decline Rep", "Improve Rep", "Steady Rep", "Junior Rep"])
    df = load_and_clean(history, mails, config)
    compute_outliers(df, config.stats)
    compute_ewma(df, config.stats)
    compute_monthly_spc(df, config.stats)
    compute_section_analysis(df, config)
    compute_binary_stats(df, config.yn_section_labels)
    compute_supervisor_stats(df)
    compute_agent_roster(df, config)
    compute_distribution(df)


def test_binary_stats_empty_when_no_yn_sections(sales_lite: TeamConfig):
    """sales_lite has no Y/N sections — binary_stats should return []."""
    history = make_history_sheet(sales_lite)
    mails = make_mails_sheet(["Steady Rep"])
    df = load_and_clean(history, mails, sales_lite)
    binary = compute_binary_stats(df, sales_lite.yn_section_labels)
    assert binary == []


# ---------------------------------------------------------------------------
# compute_monthly_summary — month chiclets (Phase A)
# ---------------------------------------------------------------------------

def _summary_df(rows: list[dict]) -> "pd.DataFrame":
    """Build a minimal df matching the load_and_clean schema for the chiclet
    computation. compute_monthly_summary only reads timestamp + overall_score,
    so the other columns can be omitted."""
    import pandas as pd
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    return df


def test_monthly_summary_empty_df_returns_zero_count_blocks():
    out = compute_monthly_summary(_summary_df([]))
    assert out["last"]["count"] == 0
    assert out["last"]["mean"] is None
    assert out["last"]["std"] is None
    assert out["current"]["count"] == 0
    assert out["current"]["mean"] is None
    assert out["current"]["std"] is None
    # year_month strings always populated so the empty chiclet still has a label
    assert out["last"]["year_month"]
    assert out["current"]["year_month"]


def test_monthly_summary_buckets_current_and_last_correctly():
    from datetime import datetime as _dt
    import pandas as pd
    now = _dt.now()
    current_p = pd.Period(now, freq="M")
    last_p = current_p - 1
    current_ym = str(current_p)
    last_ym = str(last_p)

    df = _summary_df([
        # 3 evals in current month
        {"timestamp": f"{current_ym}-05 10:00:00", "overall_score": 80.0},
        {"timestamp": f"{current_ym}-12 10:00:00", "overall_score": 90.0},
        {"timestamp": f"{current_ym}-20 10:00:00", "overall_score": 70.0},
        # 2 evals in last month
        {"timestamp": f"{last_ym}-08 10:00:00", "overall_score": 85.0},
        {"timestamp": f"{last_ym}-15 10:00:00", "overall_score": 75.0},
        # 1 eval two months back — must be ignored
        {"timestamp": f"{str(last_p - 1)}-10 10:00:00", "overall_score": 50.0},
    ])
    out = compute_monthly_summary(df)
    assert out["current"]["year_month"] == current_ym
    assert out["current"]["count"] == 3
    assert out["current"]["mean"] == 80.0
    assert out["current"]["std"] is not None and out["current"]["std"] > 0

    assert out["last"]["year_month"] == last_ym
    assert out["last"]["count"] == 2
    assert out["last"]["mean"] == 80.0
    # n=2 → sample std defined; n<2 path tested separately
    assert out["last"]["std"] is not None


def test_monthly_summary_count_one_month_has_null_std():
    """Sample std requires n >= 2 — a one-eval month returns mean but null std."""
    from datetime import datetime as _dt
    import pandas as pd
    now = _dt.now()
    current_ym = str(pd.Period(now, freq="M"))

    df = _summary_df([
        {"timestamp": f"{current_ym}-10 10:00:00", "overall_score": 88.0},
    ])
    out = compute_monthly_summary(df)
    assert out["current"]["count"] == 1
    assert out["current"]["mean"] == 88.0
    assert out["current"]["std"] is None


def test_monthly_summary_month_boundary_buckets_strictly():
    """An eval at the last second of last month bucket-belongs to last; first
    second of current month belongs to current. Guards against off-by-one in
    Period('M') discretization."""
    from datetime import datetime as _dt
    import pandas as pd
    now = _dt.now()
    current_p = pd.Period(now, freq="M")
    last_p = current_p - 1
    # Last second of last month
    last_end = last_p.end_time
    # First second of current month
    current_start = current_p.start_time

    df = _summary_df([
        {"timestamp": last_end, "overall_score": 60.0},
        {"timestamp": current_start, "overall_score": 95.0},
    ])
    out = compute_monthly_summary(df)
    assert out["last"]["count"] == 1
    assert out["last"]["mean"] == 60.0
    assert out["current"]["count"] == 1
    assert out["current"]["mean"] == 95.0


def test_monthly_summary_does_not_mutate_input_df():
    """Defensive: chiclet computation must not add the helper 'year_month'
    column to the caller's df (other compute_* functions read this df after)."""
    from datetime import datetime as _dt
    import pandas as pd
    current_ym = str(pd.Period(_dt.now(), freq="M"))
    df = _summary_df([
        {"timestamp": f"{current_ym}-05 10:00:00", "overall_score": 80.0},
    ])
    cols_before = set(df.columns)
    compute_monthly_summary(df)
    assert set(df.columns) == cols_before


# ---------------------------------------------------------------------------
# _parse_yn_cell — preserves "Not Applicable" instead of coercing to "N"
# (NumericNAOption.md scope note resolved here).
# ---------------------------------------------------------------------------

def test_parse_yn_cell_handles_each_recognized_form():
    assert _parse_yn_cell("Y") == "Y"
    assert _parse_yn_cell("Yes") == "Y"
    assert _parse_yn_cell("YES") == "Y"
    assert _parse_yn_cell("N") == "N"
    assert _parse_yn_cell("No") == "N"
    assert _parse_yn_cell("NO") == "N"


def test_parse_yn_cell_preserves_not_applicable_sentinel():
    """The bug: 'Not Applicable'.upper()[:1] == 'N' — historical NA rows
    were miscoded as failures and pulled binary-stats percentages down.
    Each canonical NA spelling must round-trip to the 'NA' sentinel."""
    assert _parse_yn_cell("Not Applicable") == "NA"
    assert _parse_yn_cell("not applicable") == "NA"
    assert _parse_yn_cell("NA") == "NA"
    assert _parse_yn_cell("N/A") == "NA"
    assert _parse_yn_cell("  Not Applicable  ") == "NA"


def test_parse_yn_cell_empty_and_unknown_to_empty():
    """Anything we don't recognize falls out of the binary denominator —
    safer than misclassifying as Y or N."""
    assert _parse_yn_cell("") == ""
    assert _parse_yn_cell("   ") == ""
    assert _parse_yn_cell("???") == ""
    # 'Maybe' starts with 'M' — not Y or N. Defaults to empty, which
    # `_compute_binary_pct` excludes from both numerator and denominator.
    assert _parse_yn_cell("Maybe") == ""


def test_load_and_clean_preserves_yn_na_through_binary_stats(sales: TeamConfig):
    """End-to-end: a sheet row holding 'Not Applicable' for a yn section
    must NOT count as a failure in the binary-stats percentage.

    Before this fix: 'Not Applicable' was coerced to 'N' inside
    load_and_clean, dragging the team's Y% down on every NA row.
    """
    from datetime import datetime
    import pandas as pd

    yn_ids = sales.yn_history_ids
    assert yn_ids, "sales fixture must declare at least one yn section"
    target_yn = yn_ids[0]

    # Two rows for Star Rep: one passed (Y), one explicit Not Applicable.
    # The Y% on target_yn must read 100.0% (1 of 1 valid), NOT 50.0%.
    rows = [["Agent Name"] + [""] * 200]  # header
    rows.append(make_history_row(
        sales, agent="Star Rep", when=datetime(2026, 4, 1, 9, 0),
        overall=90, section_scores={hid: 5 for hid in sales.numeric_history_ids},
        yn_scores={y: ("Y" if y == target_yn else "Y") for y in yn_ids},
    ))
    rows.append(make_history_row(
        sales, agent="Star Rep", when=datetime(2026, 4, 2, 9, 0),
        overall=90, section_scores={hid: 5 for hid in sales.numeric_history_ids},
        # Write the production sentinel (what sheets_service emits via
        # YN_DISPLAY['NA']) on target_yn; Y on the rest so they're valid.
        yn_scores={y: ("Not Applicable" if y == target_yn else "Y") for y in yn_ids},
    ))

    mails = make_mails_sheet(["Star Rep"])
    df = load_and_clean(rows, mails, sales)

    # The "NA" sentinel made it through load_and_clean unmolested.
    star = df[df["agent"] == "Star Rep"]
    target_vals = sorted(star[target_yn].tolist())
    assert target_vals == ["NA", "Y"], (
        f"expected ['NA', 'Y'] after load_and_clean — got {target_vals}. "
        f"'Not Applicable' was probably re-coerced to 'N' (the original bug)."
    )

    # Y% reads 100.0 (the NA row drops out of the denominator entirely).
    binary = compute_binary_stats(df, sales.yn_section_labels)
    target_stats = next(b for b in binary if b["section_id"] == target_yn)
    assert target_stats["team_pct"] == 100.0, (
        f"Y% should be 100.0 (1 Y of 1 valid), got {target_stats['team_pct']}. "
        f"NA row probably got counted as a failure."
    )


# ---------------------------------------------------------------------------
# load_and_clean — call-time initiative (PR-1) reader fallback.
#
# Phase 1 schema:    eval-time → col_eval_approved_at; call-time → col C
# Phase 2 reader:    df["timestamp"]   resolves to col_eval_approved_at on
#                                      new-shape rows, else col C (old).
#                    df["call_started"] resolves to col C on new-shape rows,
#                                      NaT on old-shape (pre-backfill).
#
# During the transition, df["timestamp"] still anchors every analytics
# consumer at eval time — chiclets/SPC/EWMA behavior must not shift.
# ---------------------------------------------------------------------------

def test_load_and_clean_reads_old_shape_row_from_col_c(sales: TeamConfig):
    """A pre-cutover row has col_eval_approved_at blank — load_and_clean
    falls back to col C for df['timestamp']. call_started is NaT
    (we don't know when this call connected; backfill will fill it)."""
    from datetime import datetime as _dt
    import pandas as pd

    header = [["Agent Name"] + [""] * 200]
    when = _dt(2026, 4, 15, 10, 30, 0)
    # Default call_started=None → old-shape row.
    rows = header + [make_history_row(sales, agent="Star Rep", when=when, overall=92)]
    mails = make_mails_sheet(["Star Rep"])
    df = load_and_clean(rows, mails, sales)

    assert not df.empty
    star = df[df["agent"] == "Star Rep"].iloc[0]
    # df["timestamp"] reads col C (eval time) on old-shape rows.
    assert star["timestamp"] == pd.Timestamp(when)
    # call_started is NaT — pre-backfill, we don't know.
    assert pd.isna(star["call_started"])


def test_load_and_clean_reads_new_shape_row_with_call_started_split(sales: TeamConfig):
    """A post-cutover row has col_eval_approved_at populated. df['timestamp']
    reads the new column (eval time); df['call_started'] reads col C
    (call connected). Verifies the split is observed correctly."""
    from datetime import datetime as _dt
    import pandas as pd

    header = [["Agent Name"] + [""] * 200]
    eval_time = _dt(2026, 5, 10, 14, 0, 0)
    call_time = _dt(2026, 5, 9, 9, 15, 0)   # call happened day before scoring
    rows = header + [make_history_row(
        sales, agent="Star Rep", when=eval_time, overall=92,
        call_started=call_time,
    )]
    mails = make_mails_sheet(["Star Rep"])
    df = load_and_clean(rows, mails, sales)

    star = df[df["agent"] == "Star Rep"].iloc[0]
    # df["timestamp"] reads col_eval_approved_at on new-shape rows.
    assert star["timestamp"] == pd.Timestamp(eval_time)
    # call_started reads col C — the day the call actually connected.
    assert star["call_started"] == pd.Timestamp(call_time)


def test_load_and_clean_mixed_shapes_coexist(sales: TeamConfig):
    """Real-world during the PR-1 → PR-2 transition: some rows are old-shape
    (pre-cutover), some are new-shape (post-cutover). df['timestamp']
    must still anchor every row at eval time so chiclets/SPC/EWMA
    don't see a mixed bucket shift before backfill completes."""
    from datetime import datetime as _dt
    import pandas as pd

    header = [["Agent Name"] + [""] * 200]
    old_when = _dt(2026, 3, 1, 12, 0, 0)
    new_eval = _dt(2026, 5, 5, 12, 0, 0)
    new_call = _dt(2026, 5, 3, 10, 0, 0)
    rows = header + [
        # Old-shape row.
        make_history_row(sales, agent="Star Rep", when=old_when, overall=88),
        # New-shape row.
        make_history_row(
            sales, agent="Steady Rep", when=new_eval, overall=82,
            call_started=new_call,
        ),
    ]
    mails = make_mails_sheet(["Star Rep", "Steady Rep"])
    df = load_and_clean(rows, mails, sales)

    star = df[df["agent"] == "Star Rep"].iloc[0]
    steady = df[df["agent"] == "Steady Rep"].iloc[0]

    # Old-shape: timestamp from col C, call_started NaT.
    assert star["timestamp"] == pd.Timestamp(old_when)
    assert pd.isna(star["call_started"])

    # New-shape: timestamp from col_eval_approved_at, call_started from col C.
    assert steady["timestamp"] == pd.Timestamp(new_eval)
    assert steady["call_started"] == pd.Timestamp(new_call)


def test_load_and_clean_analytics_still_bucket_by_eval_time_during_transition(
    sales: TeamConfig,
):
    """Critical for PR-1 staging: the call-time initiative MUST NOT shift
    chiclets/SPC bucketing until PR-3 lands. A new-shape row with a
    call-time in April and eval-time in May must surface in May's bucket
    via compute_monthly_summary — same as it would have pre-initiative."""
    from datetime import datetime as _dt
    import pandas as pd

    header = [["Agent Name"] + [""] * 200]
    eval_in_may = _dt(2026, 5, 5, 12, 0, 0)
    call_in_apr = _dt(2026, 4, 28, 10, 0, 0)  # 7 days before scoring
    rows = header + [make_history_row(
        sales, agent="Star Rep", when=eval_in_may, overall=90,
        call_started=call_in_apr,
    )]
    mails = make_mails_sheet(["Star Rep"])
    df = load_and_clean(rows, mails, sales)

    # df["timestamp"] anchored at May (eval time), NOT April (call time).
    assert df.iloc[0]["timestamp"].month == 5
    # call_started is parsed and available for PR-3 to consume.
    assert df.iloc[0]["call_started"].month == 4


# ---------------------------------------------------------------------------
# Known tech debt — long-form analytics cannot distinguish explicit N/A
# from "unscored" on numeric sections.
# ---------------------------------------------------------------------------

@pytest.mark.xfail(
    reason=(
        "TECH DEBT (NumericNAOption.md, open Q3, 2026-05-25) — "
        "compute_long_form coalesces explicit numeric N/A ('Not Applicable' "
        "in the sheet cell) to np.nan in load_and_clean, the same value used "
        "for missing/unscored. Downstream cannot count an N/A rate per "
        "section. Revisit during the SQL / deeper-analytics migration: "
        "either widen the long-form schema with an `na_flag` column or pivot "
        "to a typed Union score column."
    ),
    strict=False,
)
def test_compute_long_form_distinguishes_explicit_na_from_missing(sales: TeamConfig):
    from datetime import datetime
    import numpy as np

    history = [
        ["Agent Name"] + [""] * 100,  # header
        # Row 1: explicit N/A on first numeric section
        make_history_row(
            sales, agent="Star Rep", when=datetime(2026, 4, 1, 9, 0, 0),
            overall=90,
            section_scores={sales.numeric_history_ids[0]: "Not Applicable"},  # type: ignore[dict-item]
            yn_scores={y: "Y" for y in sales.yn_history_ids},
        ),
        # Row 2: blank/missing on the same section
        make_history_row(
            sales, agent="Star Rep", when=datetime(2026, 4, 2, 9, 0, 0),
            overall=90,
            section_scores={sales.numeric_history_ids[0]: ""},  # type: ignore[dict-item]
            yn_scores={y: "Y" for y in sales.yn_history_ids},
        ),
    ]
    mails = make_mails_sheet(["Star Rep"])
    df = load_and_clean(history, mails, sales)
    long_df = compute_long_form(df, sales)

    target_rows = long_df[long_df["section_id"] == sales.numeric_history_ids[0]]
    target_rows = target_rows[target_rows["agent"] == "Star Rep"]
    # Today both rows collapse to NaN. When the gap is closed, one row should
    # be marked NA and the other should remain missing.
    na_rows = target_rows[target_rows["score"].astype(str).str.upper() == "NA"]
    missing_rows = target_rows[target_rows["score"].isna()]
    assert len(na_rows) == 1, "explicit N/A row was not distinguishable in long form"
    assert len(missing_rows) == 1, "missing row was not preserved as NaN"


# ---------------------------------------------------------------------------
# Former failure points — now regular regression-guard tests
# ---------------------------------------------------------------------------

def test_section_analysis_priority_thresholds_scale_with_range(sales_decimal: TeamConfig):
    """FAILURE POINT #7 fixed in Phase B (b): priority thresholds scale
    with each section's score_range. A moderate gap on a 1-10 scale must
    not register as 'high priority' the way the legacy 1.5 cutoff did."""
    from datetime import datetime, timedelta

    base = datetime(2026, 4, 1)
    rows = [["Agent Name"] + [""] * 100]  # header

    # Strong cohort: scores 9 across the board
    for agent in ["Strong A", "Strong B", "Strong C", "Strong D"]:
        for i in range(4):
            rows.append(make_history_row(
                sales_decimal, agent=agent, when=base + timedelta(days=i),
                overall=85,
                section_scores={hid: 9 for hid in sales_decimal.numeric_history_ids},
                yn_scores={y: "Y" for y in sales_decimal.yn_history_ids},
            ))

    # Mediocre Rep: scores 7 on one section — gap of ~2 on a 1-10 scale.
    # Proportionally equivalent to a 0.8 gap on 1-5 — should be "low".
    target_section = sales_decimal.numeric_history_ids[0]
    for i in range(4):
        scores = {hid: 9 for hid in sales_decimal.numeric_history_ids}
        scores[target_section] = 7
        rows.append(make_history_row(
            sales_decimal, agent="Mediocre Rep", when=base + timedelta(days=i),
            overall=80, section_scores=scores,
            yn_scores={y: "Y" for y in sales_decimal.yn_history_ids},
        ))

    mails = make_mails_sheet(["Strong A", "Strong B", "Strong C", "Strong D", "Mediocre Rep"])
    df = load_and_clean(rows, mails, sales_decimal)
    out = compute_section_analysis(df, sales_decimal)
    mediocre_high = [
        op for op in out["training_opportunities"]
        if op["agent"] == "Mediocre Rep" and op["priority"] == "high"
    ]
    assert not mediocre_high, (
        f"Mediocre Rep flagged 'high' priority on a 1-10 scale — priority "
        f"thresholds did not scale with score_range"
    )


def test_history_parse_row_min_width_derives_from_layout(sales_lite: TeamConfig):
    """FAILURE POINT #8 fixed in Phase B (b): _parse_row's minimum width
    derives from history_layout.col_overall_score + 1 (= 6 cells), not
    the magic literal 15. Guard against regression."""
    from backend.services.history_service import SheetsProvider
    import inspect
    src = inspect.getsource(SheetsProvider._parse_row)
    assert "len(row) < 15" not in src, (
        "history_service.SheetsProvider._parse_row re-introduced the "
        "literal len(row) < 15 — minimum width must derive from the layout"
    )

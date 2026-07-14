#!/usr/bin/env python3
"""F2 golden-parity harness — sheet vs Postgres row-source (ReadPathFlip §5).

Read-only on both sides. Builds the analytics DataFrame from the
Analyst_History sheet (today's path: raw rows → load_and_clean) and from
qa.* (team_source.fetch_history_frame), aligns them on the common row
set, then proves two things:

A. FRAME PARITY — the common rows are cell-identical (scores exact,
   timestamps to the second, yn vocab verbatim) and all nine compute_*
   functions agree on that common subset.

B. ENDPOINT PARITY — the /team/stats, /team/evals, /team/long_form
   request pipelines (the exact filter order from routes/team.py) produce
   identical output from either source across the full
   days / range / supervisor / active_only permutation grid, plus every
   month present in the data for the /evals drill-down. This is the F2
   checkpoint: "parity green across days/range/supervisor/active_only
   permutations, both teams."

Membership deltas are expected and classified, NOT failures: sheet-only
rows (B0 hard-zero exclusions) and db-only rows (backfilled history the
sheet dropped, e.g. the Sales pre-flip archive). Parity is measured on
the common subset — everything the two sources agree exists.

Exit 0 = every checked team parity-exact; 2 = a real divergence (read the
per-team report JSON).

Usage:
    cd qa-automation/AI-Scoring
    python3 scripts/parity_readpath.py                 # both teams
    python3 scripts/parity_readpath.py --team-id sales # one team
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from datetime import datetime, time, timedelta
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

_AI_SCORING = Path(__file__).resolve().parent.parent
_STAGING_DIR = _AI_SCORING.parent.parent / "database" / "backfill_staging"
load_dotenv(_AI_SCORING / ".env")
if str(_AI_SCORING) not in sys.path:
    sys.path.insert(0, str(_AI_SCORING))

from backend.config.team_config import get_team_config  # noqa: E402
from backend.services import team_stats as ts  # noqa: E402
from backend.services.team_source import fetch_history_frame  # noqa: E402

_ALL_TEAMS = ["member_support", "sales"]


def _strip_accents(s):
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if not unicodedata.combining(c))


def _key(df):
    """String key eval_id|agent|ordinal — agent accent-stripped (roster
    spellings drift), ordinal disambiguates D2 re-eval pairs
    deterministically by (timestamp, overall_score)."""
    base = (df["eval_id"].astype(str) + "|"
            + df["agent"].str.strip().str.lower().map(_strip_accents))
    order = df.assign(_b=base).sort_values(["_b", "timestamp", "overall_score"])
    ordinal = order.groupby("_b").cumcount()
    return (base + "|" + ordinal.reindex(df.index).astype(str)).tolist()


def _norm(v):
    if isinstance(v, float) and math.isnan(v):
        return None
    if hasattr(v, "isoformat"):
        return v.isoformat(timespec="seconds")
    return v


def _common_subsets(sheet_df, pg_df):
    """Align on the eval_id|agent|ordinal key; return the membership
    classification plus the two common-subset frames (clean RangeIndex,
    identical row order — sorted by key)."""
    sheet_df = sheet_df.assign(_k=_key(sheet_df)).set_index("_k")
    pg_df = pg_df.assign(_k=_key(pg_df)).set_index("_k")
    sheet_only = sorted(set(sheet_df.index) - set(pg_df.index))
    db_only = sorted(set(pg_df.index) - set(sheet_df.index))
    common = sorted(set(sheet_df.index) & set(pg_df.index))
    membership = {
        "sheet": len(sheet_df), "pg": len(pg_df), "common": len(common),
        "sheet_only": len(sheet_only), "db_only": len(db_only),
        "sheet_only_keys": [k.split("|") for k in sheet_only[:25]],
        "db_only_sample": [k.split("|") for k in db_only[:5]],
    }
    sub_sheet = sheet_df.loc[common].reset_index(drop=True)
    sub_pg = pg_df.loc[common].reset_index(drop=True)
    return membership, sub_sheet, sub_pg


# ---------------------------------------------------------------------------
# A. Frame parity — cell diffs + compute_* on the common subset
# ---------------------------------------------------------------------------

def frame_parity(sub_sheet, sub_pg, config) -> dict:
    cols = [c for c in sub_sheet.columns if c in sub_pg.columns]
    cell_diffs = []
    for i in range(len(sub_sheet)):
        a, b = sub_sheet.iloc[i], sub_pg.iloc[i]
        for c in cols:
            va, vb = _norm(a[c]), _norm(b[c])
            if va != vb:
                cell_diffs.append({"row": i, "col": c, "sheet": va, "pg": vb})

    stats = config.stats
    computations = {
        "monthly_summary": lambda d: ts.compute_monthly_summary(d),
        "monthly_spc": lambda d: ts.compute_monthly_spc(d, stats),
        "ewma": lambda d: ts.compute_ewma(d, stats),
        "outliers": lambda d: ts.compute_outliers(d, stats),
        "distribution": lambda d: ts.compute_distribution(d),
        "section_analysis": lambda d: ts.compute_section_analysis(d, config),
        "binary_stats": lambda d: ts.compute_binary_stats(d, config.yn_section_labels),
        "supervisor_stats": lambda d: ts.compute_supervisor_stats(d),
        "roster": lambda d: ts.compute_agent_roster(d, config),
        "long_form": lambda d: ts.compute_long_form(d, config).to_dict("records"),
    }
    compute_diffs = {}
    for name, fn in computations.items():
        ja, jb = _safe(lambda: fn(sub_sheet)), _safe(lambda: fn(sub_pg))
        if ja != jb:
            compute_diffs[name] = {"sheet_len": len(ja), "pg_len": len(jb)}
    return {"cell_diffs": cell_diffs[:50], "cell_diff_count": len(cell_diffs),
            "compute_diffs": compute_diffs}


# ---------------------------------------------------------------------------
# B. Endpoint parity — mirror routes/team.py filter pipelines exactly
# ---------------------------------------------------------------------------

def _safe(thunk) -> str:
    """JSON-serialize a computation, capturing exceptions as a stable
    signature so an identical raise on both sides reads as parity, not a
    harness crash."""
    try:
        return json.dumps(thunk(), default=str, sort_keys=True)
    except Exception as e:  # noqa: BLE001 — signature comparison, not handling
        return f"__exc__:{type(e).__name__}:{e}"


def _stats_pipeline(df, config, *, active_only, supervisor, window, days, now):
    if df.empty:
        return {"empty": True}
    d = df
    if active_only:
        d = d[d["is_active"]]
    if supervisor:
        d = d[d["supervisor"].str.lower() == supervisor.strip().lower()]
    # Monthly chiclets honor active/supervisor but NOT days (team.py:139).
    monthly = ts.compute_monthly_summary(d)
    if window is not None:
        d = d[(d["timestamp"] >= window[0]) & (d["timestamp"] <= window[1])]
    elif days > 0:
        d = d[d["timestamp"] >= now - timedelta(days=days)]
    stats = config.stats
    return {
        "kpis": {
            "total": len(d),
            "avg": round(float(d["overall_score"].mean()), 1) if len(d) else 0,
            "std": round(float(d["overall_score"].std(ddof=1)), 1) if len(d) > 1 else 0,
            "analysts": int(d["agent"].nunique()),
        },
        "monthly": monthly,
        "roster": ts.compute_agent_roster(d, config),
        "outliers": ts.compute_outliers(d, stats),
        "spc": ts.compute_monthly_spc(d, stats),
        "section_analysis": ts.compute_section_analysis(d, config),
        "binary_stats": ts.compute_binary_stats(d, config.yn_section_labels),
        "supervisor_stats": ts.compute_supervisor_stats(d),
        "ewma": ts.compute_ewma(d, stats),
        "distribution": ts.compute_distribution(d),
    }


def _long_form_pipeline(df, config, *, active_only, supervisor, window, days, now):
    if df.empty:
        return []
    d = df
    # team.py:327 applies window/days BEFORE active/supervisor here.
    if window is not None:
        d = d[(d["timestamp"] >= window[0]) & (d["timestamp"] <= window[1])]
    elif days > 0:
        d = d[d["timestamp"] >= now - timedelta(days=days)]
    if active_only:
        d = d[d["is_active"]]
    if supervisor:
        d = d[d["supervisor"].str.lower() == supervisor.strip().lower()]
    return ts.compute_long_form(d, config).to_dict("records")


def _evals_pipeline(df, config, *, year_month, active_only, supervisor):
    if df.empty:
        return []
    d = df
    if active_only:
        d = d[d["is_active"]]
    if supervisor:
        d = d[d["supervisor"].str.lower() == supervisor.strip().lower()]
    months = ts._months_in_bucket_tz(d["timestamp"])
    d = d[months == year_month].sort_values("timestamp", ascending=False)
    return [
        {"agent": r["agent"], "eval_id": r["eval_id"],
         "timestamp": _norm(r["timestamp"]),
         "overall_score": float(r["overall_score"]),
         "supervisor": r.get("supervisor", ""),
         "manager_email": r.get("manager_email", "")}
        for r in d.to_dict("records")
    ]


def endpoint_parity(sub_sheet, sub_pg, config, now) -> dict:
    """Run every request permutation against both frames and diff."""
    # Representative supervisor: the one covering the most common rows.
    sup_counts = sub_sheet[sub_sheet["supervisor"] != ""]["supervisor"].value_counts()
    sup = str(sup_counts.index[0]) if len(sup_counts) else ""
    supervisors = ["", sup] if sup else [""]

    # Representative custom range: the middle 50% of the data span.
    ts_min, ts_max = sub_sheet["timestamp"].min(), sub_sheet["timestamp"].max()
    span = ts_max - ts_min
    r_from = (ts_min + span * 0.25).date()
    r_to = (ts_min + span * 0.75).date()
    rng = (datetime.combine(r_from, time.min), datetime.combine(r_to, time.max))
    windows = [("days30", None, 30), ("days90", None, 90),
               ("days730", None, 730), ("range", rng, 0)]

    combos, failures = [], []

    def check(tag, fa, fb):
        combos.append(tag)
        ja, jb = _safe(fa), _safe(fb)
        if ja != jb:
            failures.append({"combo": tag, "sheet": ja[:240], "pg": jb[:240]})

    for ao in (True, False):
        for su in supervisors:
            for wname, win, days in windows:
                k = dict(active_only=ao, supervisor=su, window=win, days=days, now=now)
                sfx = f"[active={ao},sup={bool(su)},{wname}]"
                check("stats" + sfx,
                      lambda k=k: _stats_pipeline(sub_sheet, config, **k),
                      lambda k=k: _stats_pipeline(sub_pg, config, **k))
                check("long_form" + sfx,
                      lambda k=k: _long_form_pipeline(sub_sheet, config, **k),
                      lambda k=k: _long_form_pipeline(sub_pg, config, **k))

    months = sorted(set(ts._months_in_bucket_tz(sub_sheet["timestamp"])))
    for ym in months:
        for ao in (True, False):
            for su in supervisors:
                k = dict(year_month=ym, active_only=ao, supervisor=su)
                check(f"evals[{ym},active={ao},sup={bool(su)}]",
                      lambda k=k: _evals_pipeline(sub_sheet, config, **k),
                      lambda k=k: _evals_pipeline(sub_pg, config, **k))

    return {"supervisor_probe": sup, "range_probe": [str(r_from), str(r_to)],
            "months": months, "combos_run": len(combos),
            "failures": failures[:50], "failure_count": len(failures)}


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

async def run_team(team_id: str) -> int:
    config = get_team_config(team_id)
    from backend.services.data_provider import get_provider
    provider = await get_provider(team_id)
    sheet_df = ts.load_and_clean(
        provider._ws.get_all_values(), provider._get_mails_sheet(), config)
    pg_df = await fetch_history_frame(config)

    if sheet_df.empty or pg_df.empty:
        print(f"[parity:{team_id}] ABORT — empty frame "
              f"(sheet={len(sheet_df)} pg={len(pg_df)})")
        return 2

    membership, sub_sheet, sub_pg = _common_subsets(sheet_df, pg_df)
    frame = frame_parity(sub_sheet, sub_pg, config)
    # Fixed reference time so both frames see the same days-cutoffs.
    endpoints = endpoint_parity(sub_sheet, sub_pg, config, datetime.now())

    report = {"membership": membership, "frame": frame, "endpoints": endpoints}
    report_path = _STAGING_DIR / f"report_{team_id}_parity.json"
    report_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    m = membership
    print(f"[parity:{team_id}] sheet={m['sheet']} pg={m['pg']} common={m['common']} "
          f"sheet_only={m['sheet_only']} db_only={m['db_only']}")
    print(f"  A/frame:    cell_diffs={frame['cell_diff_count']} "
          f"compute_diffs={list(frame['compute_diffs']) or 'NONE'}")
    print(f"  B/endpoint: {endpoints['combos_run']} combos, "
          f"{endpoints['failure_count']} failed "
          f"{[f['combo'] for f in endpoints['failures'][:6]] or ''}")
    print(f"  report: {report_path}")

    ok = (not frame["cell_diff_count"] and not frame["compute_diffs"]
          and not endpoints["failure_count"])
    return 0 if ok else 2


async def run(args) -> int:
    teams = [args.team_id] if args.team_id else _ALL_TEAMS
    codes = [await run_team(t) for t in teams]
    return 0 if all(c == 0 for c in codes) else 2


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--team-id", default=None,
                    help="one team; omit to sweep both (member_support, sales)")
    args = ap.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())

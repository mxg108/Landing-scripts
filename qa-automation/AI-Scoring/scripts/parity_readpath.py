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
import sys
from datetime import datetime, time, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from dotenv import load_dotenv

_AI_SCORING = Path(__file__).resolve().parent.parent
_STAGING_DIR = _AI_SCORING.parent.parent / "database" / "backfill_staging"
load_dotenv(_AI_SCORING / ".env")
if str(_AI_SCORING) not in sys.path:
    sys.path.insert(0, str(_AI_SCORING))

from backend.config.team_config import get_team_config  # noqa: E402
from backend.services import team_stats as ts  # noqa: E402
from backend.services.read_path_shadow import (  # noqa: E402
    align, cell_diffs as _cell_diffs, classify_cell_diff, classify_diffs,
    norm_cell as _norm,
)
from backend.services.team_source import fetch_history_frame  # noqa: E402

_ALL_TEAMS = ["member_support", "sales"]


# ---------------------------------------------------------------------------
# A. Frame parity — cell diffs + compute_* on the common subset
# ---------------------------------------------------------------------------

def reconcile_benign(sub_sheet, sub_pg, diffs):
    """Return a copy of sub_pg with every KNOWN source-of-truth cell
    (clock / name_accent / roster) overwritten by the sheet value, leaving
    'other' cells intact. Running compute_* / endpoints on this isolates
    genuine row-source divergence from the classified deltas — so the
    verdict measures the flip's correctness, not qa.*'s clock/roster
    currency (those are reported separately as data actions)."""
    out = sub_pg.copy()
    for d in diffs:
        if classify_cell_diff(d) != "other":
            out.at[d["row"], d["col"]] = sub_sheet.at[d["row"], d["col"]]
    return out


def frame_parity(sub_sheet, sub_pg_recon, config, diffs) -> dict:
    """compute_* parity on the reconciled frames + the cell-diff
    classification. `diffs` are the RAW (pre-reconcile) cell diffs."""
    buckets = classify_diffs(diffs)
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
        ja, jb = _safe(lambda: fn(sub_sheet)), _safe(lambda: fn(sub_pg_recon))
        if ja != jb:
            compute_diffs[name] = {"sheet_len": len(ja), "pg_len": len(jb)}

    result = {
        "cell_diff_count": len(diffs),
        "classified": {k: len(v) for k, v in buckets.items()},
        "classified_samples": {k: v[:5] for k, v in buckets.items() if v},
        "unexplained": buckets["other"][:50],
        # Column-set mismatch is invisible to cell diffs (they compare only
        # shared columns) but drives compute_long_form (it emits per-column).
        "col_only_sheet": sorted(set(sub_sheet.columns) - set(sub_pg_recon.columns)),
        "col_only_pg": sorted(set(sub_pg_recon.columns) - set(sub_sheet.columns)),
        "compute_diffs": compute_diffs,
    }
    if "long_form" in compute_diffs:
        result["long_form_detail"] = _long_form_detail(sub_sheet, sub_pg_recon, config)
    return result


def _long_form_detail(sub_sheet, sub_pg, config) -> dict:
    """Pinpoint the long_form divergence: row counts, the first differing
    record as RAW json (so a number-vs-string serialization shows), and
    the score-column dtypes per source (the dtype smoking gun)."""
    ra = ts.compute_long_form(sub_sheet, config).to_dict("records")
    rb = ts.compute_long_form(sub_pg, config).to_dict("records")
    sec_ids = list(config.numeric_history_ids)
    detail = {
        "sheet_rows": len(ra), "pg_rows": len(rb),
        "sheet_dtypes": {c: str(sub_sheet[c].dtype) for c in sec_ids if c in sub_sheet},
        "pg_dtypes": {c: str(sub_pg[c].dtype) for c in sec_ids if c in sub_pg},
    }
    for i in range(min(len(ra), len(rb))):
        ja = json.dumps(ra[i], default=_json_default, sort_keys=True)
        jb = json.dumps(rb[i], default=_json_default, sort_keys=True)
        if ja != jb:
            detail["first_diff_index"] = i
            detail["sheet_rec_json"] = ja
            detail["pg_rec_json"] = jb
            break
    return detail


# ---------------------------------------------------------------------------
# B. Endpoint parity — mirror routes/team.py filter pipelines exactly
# ---------------------------------------------------------------------------

def _json_default(o):
    """Serialization normalizer for the compute-parity comparison, matching
    the cell comparison's granularity (read_path_shadow.norm_cell):

    - datetimes/Timestamps → second-ISO. compute_long_form (the only
      compute that emits RAW frame timestamps, unaggregated) otherwise
      serializes the DB's microsecond value (e.g. ...:21.479) against the
      sheet's strftime-truncated ...:21 → a spurious diff on every row,
      even when cell_diffs (already second-granular) sees none. This is
      the Sales long_form divergence: 0 cell diffs, yet long_form differed.
    - numpy scalars → Python equivalents so np.float64(4.0) and float(4.0)
      compare EQUAL rather than "4.0" (str) vs 4.0 (number).
    """
    if hasattr(o, "isoformat"):
        try:
            return o.isoformat(timespec="seconds")
        except TypeError:  # a date (no timespec) — still stable
            return o.isoformat()
    if isinstance(o, np.generic):
        return o.item()
    return str(o)


def _safe(thunk) -> str:
    """JSON-serialize a computation, capturing exceptions as a stable
    signature so an identical raise on both sides reads as parity, not a
    harness crash."""
    try:
        return json.dumps(thunk(), default=_json_default, sort_keys=True)
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
    # Direct SheetsProvider — the get_provider factory is Postgres-only
    # since F5; the sheet side of this comparison is exactly the retired
    # read path, kept alive here while the sheet is still written as a
    # projection.
    from backend.services.history_service import SheetsProvider
    provider = SheetsProvider(config=config)
    sheet_df = ts.load_and_clean(
        provider._ws.get_all_values(), provider._get_mails_sheet(), config)
    pg_df = await fetch_history_frame(config)

    if sheet_df.empty or pg_df.empty:
        print(f"[parity:{team_id}] ABORT — empty frame "
              f"(sheet={len(sheet_df)} pg={len(pg_df)})")
        return 2

    membership, sub_sheet, sub_pg = align(sheet_df, pg_df)
    diffs = _cell_diffs(sub_sheet, sub_pg)
    # Reconcile the KNOWN source-of-truth deltas so compute_* + endpoint
    # parity measure the FLIP's correctness, not qa.*'s clock/roster
    # currency (reported separately below as data actions).
    sub_pg_recon = reconcile_benign(sub_sheet, sub_pg, diffs)
    frame = frame_parity(sub_sheet, sub_pg_recon, config, diffs)
    # Fixed reference time so both frames see the same days-cutoffs.
    endpoints = endpoint_parity(sub_sheet, sub_pg_recon, config, datetime.now())

    # Agents whose roster cells (is_active/supervisor) are stale in qa.* —
    # the actionable list: import_agents.py --team <id> refreshes them.
    roster_agents = sorted({
        str(sub_sheet.at[d["row"], "agent"]) for d in diffs
        if classify_cell_diff(d) == "roster"
    })
    cls = frame["classified"]

    report = {"membership": membership, "frame": frame, "endpoints": endpoints,
              "roster_stale_agents": roster_agents}
    report_path = _STAGING_DIR / f"report_{team_id}_parity.json"
    report_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    m = membership
    print(f"[parity:{team_id}] sheet={m['sheet']} pg={m['pg']} common={m['common']} "
          f"sheet_only={m['sheet_only']} db_only={m['db_only']}")
    print(f"  cell diffs: {frame['cell_diff_count']} → "
          f"clock={cls['clock']} name_accent={cls['name_accent']} "
          f"roster={cls['roster']} | OTHER (unexplained)={cls['other']}")
    print(f"  A/frame (reconciled):    compute_diffs="
          f"{list(frame['compute_diffs']) or 'NONE'}")
    print(f"  B/endpoint (reconciled): {endpoints['combos_run']} combos, "
          f"{endpoints['failure_count']} failed "
          f"{[f['combo'] for f in endpoints['failures'][:6]] or ''}")
    if roster_agents:
        print(f"  ACTION — refresh qa.agents ({len(roster_agents)} stale): "
              f"import_agents.py --team {team_id}  ⟶  {roster_agents[:8]}"
              f"{' …' if len(roster_agents) > 8 else ''}")
    if cls["other"]:
        print(f"  UNEXPLAINED diffs (investigate): {frame['unexplained'][:5]}")
    if frame["col_only_sheet"] or frame["col_only_pg"]:
        print(f"  COLUMN MISMATCH — sheet-only={frame['col_only_sheet']} "
              f"pg-only={frame['col_only_pg']}")
    if "long_form_detail" in frame:
        d = frame["long_form_detail"]
        print(f"  long_form: sheet_rows={d['sheet_rows']} pg_rows={d['pg_rows']} "
              f"first_diff@{d.get('first_diff_index')}")
        if d["sheet_dtypes"] != d["pg_dtypes"]:
            mism = {c: (d["sheet_dtypes"].get(c), d["pg_dtypes"].get(c))
                    for c in d["sheet_dtypes"]
                    if d["sheet_dtypes"].get(c) != d["pg_dtypes"].get(c)}
            print(f"    DTYPE MISMATCH (sheet,pg): {mism}")
        if "sheet_rec_json" in d:
            print(f"    sheet_rec={d['sheet_rec_json']}")
            print(f"    pg_rec   ={d['pg_rec_json']}")
    print(f"  report: {report_path}")

    # Green = every divergence is a KNOWN class (clock/name_accent/roster)
    # and the reconciled compute_* + endpoints agree exactly.
    ok = (not cls["other"] and not frame["compute_diffs"]
          and not endpoints["failure_count"])
    print(f"  verdict: {'GREEN — row-source correct' if ok else 'RED — unexplained divergence'}")
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

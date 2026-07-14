#!/usr/bin/env python3
"""F2 golden-parity harness — sheet vs Postgres row-source (ReadPathFlip §5).

Read-only on both sides. Builds the analytics DataFrame from the
Analyst_History sheet (today's path: raw rows → load_and_clean) and from
qa.* (team_source.fetch_history_frame), then:

1. aligns rows on (eval_id, agent-lower) and classifies membership
   deltas — sheet-only rows (expected: the B0 hard-zero exclusions),
   db-only rows (expected: backfilled history the sheet dropped, e.g.
   the entire Sales pre-flip archive);
2. cell-diffs the COMMON rows column by column (scores exact,
   timestamps to the second, yn vocab verbatim);
3. runs all nine compute_* functions on the common subset of BOTH
   frames and deep-diffs their JSON.

Exit 0 = common-set parity exact; 2 = differences beyond the classified
membership deltas (read the report).

Usage:
    cd qa-automation/AI-Scoring
    python3 scripts/parity_readpath.py --team-id member_support
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from pathlib import Path

from dotenv import load_dotenv

_AI_SCORING = Path(__file__).resolve().parent.parent
_STAGING_DIR = _AI_SCORING.parent.parent / "database" / "backfill_staging"
load_dotenv(_AI_SCORING / ".env")
if str(_AI_SCORING) not in sys.path:
    sys.path.insert(0, str(_AI_SCORING))

from backend.config.team_config import get_team_config  # noqa: E402
from backend.services import team_stats as ts  # noqa: E402
from backend.services.team_source import fetch_history_frame  # noqa: E402


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


def compare_frames(sheet_df, pg_df, config) -> dict:
    sheet_df = sheet_df.assign(_k=_key(sheet_df)).set_index("_k")
    pg_df = pg_df.assign(_k=_key(pg_df)).set_index("_k")
    sheet_only = sorted(set(sheet_df.index) - set(pg_df.index))
    db_only = sorted(set(pg_df.index) - set(sheet_df.index))
    common = sorted(set(sheet_df.index) & set(pg_df.index))

    cols = [c for c in sheet_df.columns if c in pg_df.columns]
    cell_diffs = []
    for k in common:
        a, b = sheet_df.loc[k], pg_df.loc[k]
        for c in cols:
            va, vb = _norm(a[c]), _norm(b[c])
            if va != vb:
                cell_diffs.append({"key": list(k), "col": c,
                                   "sheet": va, "pg": vb})

    # compute_* parity on the common subset
    sub_sheet = sheet_df.loc[common].reset_index(drop=True)
    sub_pg = pg_df.loc[common].reset_index(drop=True)
    stats = config.stats
    compute_diffs = {}
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
    for name, fn in computations.items():
        ja = json.dumps(fn(sub_sheet), default=str, sort_keys=True)
        jb = json.dumps(fn(sub_pg), default=str, sort_keys=True)
        if ja != jb:
            compute_diffs[name] = {"sheet_len": len(ja), "pg_len": len(jb)}

    return {"rows": {"sheet": len(sheet_df), "pg": len(pg_df),
                     "common": len(common), "sheet_only": len(sheet_only),
                     "db_only": len(db_only)},
            "sheet_only_keys": [list(k) for k in sheet_only[:25]],
            "db_only_sample": [list(k) for k in db_only[:5]],
            "cell_diffs": cell_diffs[:50],
            "cell_diff_count": len(cell_diffs),
            "compute_diffs": compute_diffs}


async def run(args) -> int:
    config = get_team_config(args.team_id)
    from backend.services.data_provider import get_provider
    provider = await get_provider(args.team_id)
    history_rows = provider._ws.get_all_values()
    mails_rows = provider._get_mails_sheet()
    sheet_df = ts.load_and_clean(history_rows, mails_rows, config)
    pg_df = await fetch_history_frame(config)

    result = compare_frames(sheet_df, pg_df, config)
    report_path = _STAGING_DIR / f"report_{args.team_id}_parity.json"
    report_path.write_text(json.dumps(result, indent=2, default=str),
                           encoding="utf-8")

    r = result["rows"]
    print(f"[parity:{args.team_id}] sheet={r['sheet']} pg={r['pg']} "
          f"common={r['common']} sheet_only={r['sheet_only']} "
          f"db_only={r['db_only']}")
    print(f"  common-set cell diffs: {result['cell_diff_count']}")
    print(f"  compute_* diffs: {list(result['compute_diffs']) or 'NONE'}")
    print(f"  report: {report_path}")
    return 0 if not result["cell_diff_count"] and not result["compute_diffs"] else 2


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--team-id", required=True)
    args = ap.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())

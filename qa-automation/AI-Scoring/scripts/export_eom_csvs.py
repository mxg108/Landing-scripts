#!/usr/bin/env python3
"""R2 — end-of-month CSV exports + sampling counter (JulyR2R3 §1).

One operator-run pass per team+month. Sources qa.* through the SAME
row-source + bucket-TZ month semantics as the dashboard
(team_source.fetch_history_frame + _months_in_bucket_tz), so every CSV
row count equals the month drill-down count.

Outputs under <out-dir>/<team>/<YYYY-MM>/ (UTF-8-BOM so Excel opens
them clean):
  team_summary.csv   one row per agent — evals vs the sampling target
                     (July band: 20–25/agent/month), mean/std/min/max,
                     numeric-section means, Y/N-section pcts
  agent_<slug>.csv   one per agent — one row per eval
  run_report.json    counts + parameters (staging-report convention)

Usage:
    cd qa-automation/AI-Scoring
    python3 scripts/export_eom_csvs.py --team member_support --month 2026-07 \\
        [--target 20] [--include-departed] [--out-dir ../../database/eom_exports]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

_AI_SCORING = Path(__file__).resolve().parent.parent
_DEFAULT_OUT = _AI_SCORING.parent.parent / "database" / "eom_exports"
load_dotenv(_AI_SCORING / ".env")
if str(_AI_SCORING) not in sys.path:
    sys.path.insert(0, str(_AI_SCORING))

from backend.config.team_config import get_team_config  # noqa: E402
from backend.services.data_normalization import strip_accents  # noqa: E402
from backend.services.team_source import fetch_history_frame  # noqa: E402
from backend.services.team_stats import (  # noqa: E402
    _compute_binary_pct,
    _months_in_bucket_tz,
)

_DIALPAD_URL = "https://dialpad.com/callhistory/callreview/{}"


def agent_slug(name: str) -> str:
    return "_".join(strip_accents(name).lower().split())


def month_frame(df: pd.DataFrame, month: str, include_departed: bool) -> pd.DataFrame:
    """Filter the analytics frame to one bucket-TZ month (+ active lens)."""
    if df.empty:
        return df
    d = df[_months_in_bucket_tz(df["timestamp"]) == month]
    if not include_departed:
        d = d[d["is_active"]]
    return d


def team_summary(df_month: pd.DataFrame, config, target: int) -> pd.DataFrame:
    """One row per agent — the sampling counter + score rollup."""
    numeric_ids = [h for h in config.numeric_history_ids if h in df_month.columns]
    yn_ids = [h for h in config.yn_history_ids if h in df_month.columns]
    labels = config.section_labels
    yn_labels = config.yn_section_labels

    rows = []
    for agent, g in df_month.groupby("agent"):
        scores = g["overall_score"]
        row = {
            "agent": agent,
            "supervisor": g["supervisor"].iloc[0],
            "evals": len(g),
            "sampling_target": target,
            "pct_of_target": round(100.0 * len(g) / target, 1) if target else "",
            "mean": round(float(scores.mean()), 1),
            "std": round(float(scores.std(ddof=1)), 1) if len(scores) > 1 else "",
            "min": round(float(scores.min()), 1),
            "max": round(float(scores.max()), 1),
        }
        for h in numeric_ids:
            vals = g[h].dropna()
            row[labels.get(h, h)] = round(float(vals.mean()), 2) if len(vals) else ""
        for h in yn_ids:
            row[f"{yn_labels.get(h, h)} %"] = _compute_binary_pct(g[h])
        rows.append(row)
    out = pd.DataFrame(rows)
    # roster convention: strongest first
    return out.sort_values("mean", ascending=False).reset_index(drop=True) if len(out) else out


def agent_detail(df_month: pd.DataFrame, agent: str, config) -> pd.DataFrame:
    """One row per eval for one agent, call-date ordered."""
    numeric_ids = [h for h in config.numeric_history_ids if h in df_month.columns]
    yn_ids = [h for h in config.yn_history_ids if h in df_month.columns]
    labels = {**config.section_labels, **config.yn_section_labels}

    g = df_month[df_month["agent"] == agent].sort_values("timestamp")
    rows = []
    for r in g.to_dict("records"):
        row = {
            "call_date": r["timestamp"].strftime("%Y-%m-%d %H:%M"),
            "overall_score": r["overall_score"],
        }
        for h in [*numeric_ids, *yn_ids]:
            v = r.get(h, "")
            row[labels.get(h, h)] = "" if pd.isna(v) else v
        row["evaluator"] = r.get("manager_email", "")
        row["dialpad_link"] = _DIALPAD_URL.format(r["eval_id"]) if r.get("eval_id") else ""
        rows.append(row)
    return pd.DataFrame(rows)


async def run(args) -> int:
    config = get_team_config(args.team)
    df = await fetch_history_frame(config)
    dm = month_frame(df, args.month, args.include_departed)
    if dm.empty:
        print(f"[eom:{args.team}] no rows for {args.month} — nothing exported")
        return 2

    out_dir = Path(args.out_dir) / args.team / args.month
    out_dir.mkdir(parents=True, exist_ok=True)

    summary = team_summary(dm, config, args.target)
    summary.to_csv(out_dir / "team_summary.csv", index=False, encoding="utf-8-sig")

    agents = sorted(dm["agent"].unique())
    for agent in agents:
        detail = agent_detail(dm, agent, config)
        detail.to_csv(out_dir / f"agent_{agent_slug(agent)}.csv",
                      index=False, encoding="utf-8-sig")

    report = {
        "stage": "eom_export", "team_id": args.team, "month": args.month,
        "sampling_target": args.target,
        "include_departed": args.include_departed,
        "rows_in_month": int(len(dm)),
        "agents": len(agents),
        "below_target": [r["agent"] for r in summary.to_dict("records")
                         if r["evals"] < args.target],
        "out_dir": str(out_dir),
    }
    (out_dir / "run_report.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8")

    print(f"[eom:{args.team}] {args.month}: {len(dm)} evals across "
          f"{len(agents)} agents → {out_dir}")
    below = report["below_target"]
    print(f"  below target ({args.target}): {len(below)} agent(s)"
          + (f" — {below[:8]}{' …' if len(below) > 8 else ''}" if below else ""))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--team", "--team-id", dest="team", required=True)
    ap.add_argument("--month", required=True, help="YYYY-MM (bucket-TZ month)")
    ap.add_argument("--target", type=int, default=20,
                    help="sampling target per agent per month (July band 20–25)")
    ap.add_argument("--include-departed", action="store_true")
    ap.add_argument("--out-dir", default=str(_DEFAULT_OUT))
    args = ap.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())

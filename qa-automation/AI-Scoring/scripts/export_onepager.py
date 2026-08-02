#!/usr/bin/env python3
"""Landing-branded agent one-pager — print-ready HTML (JulyR2R3 §3).

Sources qa.evaluations through the post-flip row-source (same bucket-TZ
month semantics as the dashboard) and fills the AI-Assessment slot from
qa.assessments — generating + persisting a calendar-month assessment via
the R3 writer when none exists (``--no-generate`` skips that for
cost-free re-renders). One HTML per agent, print-to-PDF ready (Letter
portrait).

The render machinery lives in ``backend/services/onepager.py`` (shared
with the dashboard's on-demand one-pager route); this script is the
operator CLI that writes the monthly files and is the only caller that
generates assessments.

Usage:
    cd qa-automation/AI-Scoring
    python3 scripts/export_onepager.py --team member_support --month 2026-06 \\
        [--agent "Name"] [--no-generate] [--include-departed] \\
        [--out-dir ../../database/eom_exports]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

_AI_SCORING = Path(__file__).resolve().parent.parent
_DEFAULT_OUT = _AI_SCORING.parent.parent / "database" / "eom_exports"
load_dotenv(_AI_SCORING / ".env")
if str(_AI_SCORING) not in sys.path:
    sys.path.insert(0, str(_AI_SCORING))


async def run(args) -> int:
    from backend.config.team_config import get_team_config
    from backend.services.assessment_store import get_or_generate_month_assessment
    from backend.services.onepager import frame_to_render, render
    from backend.services.team_source import fetch_history_frame
    from backend.services.team_stats import _months_in_bucket_tz

    config = get_team_config(args.team)
    df = await fetch_history_frame(config)
    dm = df[_months_in_bucket_tz(df["timestamp"]) == args.month] if not df.empty else df
    if not args.include_departed and not dm.empty:
        dm = dm[dm["is_active"]]
    if dm.empty:
        print(f"[onepager:{args.team}] no rows for {args.month}")
        return 2

    agents = [args.agent] if args.agent else sorted(dm["agent"].unique())
    out_dir = Path(args.out_dir) / args.team / args.month / "onepagers"
    out_dir.mkdir(parents=True, exist_ok=True)

    written = 0
    for agent in agents:
        rdf, section_cols = frame_to_render(dm, agent, config)
        if rdf.empty:
            print(f"  {agent}: no rows — skipped")
            continue
        assessment = await get_or_generate_month_assessment(
            config, agent, args.month, generate=not args.no_generate)
        slug = "_".join(agent.replace("/", " ").split())
        out = out_dir / f"onepager_{slug}_{args.month}.html"
        out.write_text(render(rdf, agent, args.month, section_cols, assessment,
                              team_label=config.display_name),
                       encoding="utf-8")
        written += 1
        print(f"  {agent}: {len(rdf)} evals"
              + (", assessment ✓" if assessment else ", assessment —")
              + f" → {out.name}")

    print(f"[onepager:{args.team}] {args.month}: {written} page(s) → {out_dir}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--team", "--team-id", dest="team", required=True)
    ap.add_argument("--month", required=True, help="YYYY-MM (bucket-TZ month)")
    ap.add_argument("--agent", default=None, help="one agent; omit for all")
    ap.add_argument("--no-generate", action="store_true",
                    help="never call Gemini — reserved slot shows a note when "
                         "no assessment is persisted")
    ap.add_argument("--include-departed", action="store_true")
    ap.add_argument("--out-dir", default=str(_DEFAULT_OUT))
    args = ap.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""C4 — Stats-API disposition backfill (DispositionDesign §7), CLI.

Thin wrapper over ``backend/services/disposition_pull.py`` — the same
fetch/parse/fill the in-app 30-min loop runs (CC_STATS_PULL_INTERVAL_MIN
on Railway). Use this for manual ranges, historic backfill, offline
CSVs, and dry runs.

The export is the ``dispositions`` records export (`Category~Sub` in a
``disposition`` column, join key ``call_id``). Filling is UPSERT:
calls rows are CREATED (seen_via='stats_pull') when the webhook era
hasn't seen the call — requires migration 017. Re-runs are idempotent
by construction (ON CONFLICT + disposition_source IS NULL guard).

Usage:
    cd qa-automation/AI-Scoring
    python3 scripts/pull_dispositions.py --days-start 20 --days-end 1 --dry-run
    python3 scripts/pull_dispositions.py --days-start 20 --days-end 1
    python3 scripts/pull_dispositions.py --is-today       # the loop's mode
    python3 scripts/pull_dispositions.py --csv /path/to/export.csv
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

_AI_SCORING = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_AI_SCORING))
load_dotenv(_AI_SCORING / ".env")

from backend.services.disposition_pull import (  # noqa: E402
    StatsRecord,
    fetch_export,
    fill_records,
    parse_export_csv,
    pull_targets,
    pull_team,
    split_disposition,
)

__all__ = ["StatsRecord", "parse_export_csv", "split_disposition", "fill_records"]


def print_report(team_id: str, report: dict, *, dry_run: bool) -> None:
    label = "DRY-RUN (would write)" if dry_run else "written"
    total = report["rows_in_export"]
    with_disp = report["with_disposition"]
    pct = (100 * with_disp / total) if total else 0.0
    print()
    print(f"=== pull_dispositions report — {team_id} ===")
    print(f"rows in export:         {total}")
    print(f"with disposition:       {with_disp} ({pct:.0f}% coverage)")
    print(f"cc.calls {label}:       {report['calls_written']}")
    print(f"qa.evaluations filled:  {report['evals_filled']}")
    print(f"row failures:           {report['row_failures']}")


async def _run(args: argparse.Namespace) -> int:
    team_id = args.team_id or pull_team()
    if args.csv:
        records = parse_export_csv(Path(args.csv).read_text(encoding="utf-8"))
    else:
        records = []
        for target in pull_targets():
            print(f"fetching dispositions export for call center {target} ...")
            text = await fetch_export(
                target,
                is_today=args.is_today,
                days_start=args.days_start,
                days_end=args.days_end,
            )
            records.extend(parse_export_csv(text))

    report = await fill_records(team_id, records, dry_run=args.dry_run)
    print_report(team_id, report, dry_run=args.dry_run)
    return 2 if report["row_failures"] else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--team-id", default=None,
                        help="default: CC_STATS_PULL_TEAM or member_support")
    parser.add_argument("--days-start", type=int, default=20,
                        help="days ago the range STARTS (older bound)")
    parser.add_argument("--days-end", type=int, default=1,
                        help="days ago the range ENDS (newer bound)")
    parser.add_argument("--is-today", action="store_true",
                        help="today's calls only (the periodic loop's mode)")
    parser.add_argument("--csv", default=None,
                        help="local export CSV — skip the Stats API")
    parser.add_argument("--dry-run", action="store_true")
    return asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())

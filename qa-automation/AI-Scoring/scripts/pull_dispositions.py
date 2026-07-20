#!/usr/bin/env python3
"""C4 — Stats-API disposition backfill (DispositionDesign §7).

Initiates a Dialpad Stats **records** export per call center + date
range, polls, downloads the CSV, joins on ``call_id`` (same id-space as
our ``dialpad_call_id``), and fills the disposition columns on BOTH
tables with ``disposition_source='stats_pull'`` — only for rows the
webhook era predates:

- ``command_center.calls``: filled only where ``disposition_source IS
  NULL`` (a live webhook stamp always wins the seam).
- ``qa.evaluations``: filled only where ``dialpad_disposition_category
  IS NULL``, joined by the triple key (per-leg / entry-point / master —
  the eval usually carries the entry-point id).

The 2026-07-15 sample export carries NO AI-CSAT column, so this script
fills dispositions only; ``ai_csat`` stays webhook-sourced (§9 appendix).

Also the catch-up sweep if the receiver ever drops events — re-runs are
idempotent by construction (the NULL guards).

Overnight contract (B-series): row failures are caught, logged, and
reported; the run never dies mid-batch. Exit 0 = clean; 2 = ran with
failures (read the report); 1 = structural (nothing written).

Usage:
    cd qa-automation/AI-Scoring
    python3 scripts/pull_dispositions.py --team-id member_support \
        --days-start 30 --days-end 0 --dry-run
    python3 scripts/pull_dispositions.py --team-id member_support \
        --days-start 30 --days-end 0
    python3 scripts/pull_dispositions.py --team-id member_support \
        --csv /path/to/export.csv        # offline: skip the API
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

_AI_SCORING = Path(__file__).resolve().parent.parent
_REPO_ROOT = _AI_SCORING.parent.parent
load_dotenv(_AI_SCORING / ".env")

import os  # noqa: E402

import httpx  # noqa: E402

BASE_URL = "https://dialpad.com/api/v2"
_CC_CONFIG = _REPO_ROOT / "command_center" / "config" / "command_center.json"

POLL_INTERVAL_S = 5
POLL_TIMEOUT_S = 600


# ---------------------------------------------------------------------------
# Pure helpers — testable without API or DB
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StatsRecord:
    call_id: str
    disposition_category: Optional[str]
    disposition: Optional[str]


def split_disposition(label: str) -> tuple[Optional[str], Optional[str]]:
    """`Category~Subdisposition` → pair; bare category → (category, None)."""
    label = (label or "").strip()
    if not label:
        return (None, None)
    category, _, sub = label.partition("~")
    return (category.strip(), sub.strip() or None)


def parse_export_csv(text: str) -> list[StatsRecord]:
    """Rows of the Stats records export we act on. Rows without a call_id
    are dropped (unjoinable); rows without a disposition are kept — they
    are the coverage denominator (§1: absence is expected-normal)."""
    records = []
    for row in csv.DictReader(io.StringIO(text)):
        call_id = (row.get("call_id") or "").strip()
        if not call_id:
            continue
        category, sub = split_disposition(row.get("disposition") or "")
        records.append(StatsRecord(call_id, category, sub))
    return records


def team_call_center_ids(team_id: str) -> list[str]:
    config = json.loads(_CC_CONFIG.read_text(encoding="utf-8"))
    team = config.get("teams", {}).get(team_id)
    if not team:
        sys.exit(f"ERROR: team '{team_id}' not in {_CC_CONFIG}")
    ids = team.get("dialpad", {}).get("target_call_center_ids", [])
    if not ids:
        sys.exit(f"ERROR: no target_call_center_ids for '{team_id}'")
    return [str(i) for i in ids]


# ---------------------------------------------------------------------------
# Stats API — initiate / poll / download
# ---------------------------------------------------------------------------


def fetch_export(call_center_id: str, days_start: int, days_end: int) -> str:
    api_key = os.environ.get("DIALPAD_API_KEY", "")
    if not api_key:
        sys.exit("ERROR: DIALPAD_API_KEY not set (env or AI-Scoring/.env).")
    headers = {"Authorization": f"Bearer {api_key}"}

    with httpx.Client(headers=headers, timeout=30) as client:
        resp = client.post(
            f"{BASE_URL}/stats",
            json={
                "export_type": "records",
                "stat_type": "calls",
                "target_id": int(call_center_id),
                "target_type": "callcenter",
                "days_ago_start": days_start,
                "days_ago_end": days_end,
                "timezone": "UTC",
            },
        )
        resp.raise_for_status()
        request_id = resp.json()["request_id"]
        print(f"stats export initiated: request_id={request_id}")

        deadline = time.monotonic() + POLL_TIMEOUT_S
        while True:
            status = client.get(f"{BASE_URL}/stats/{request_id}")
            status.raise_for_status()
            body = status.json()
            if body.get("status") == "complete":
                url = body["download_url"]
                break
            if body.get("status") == "failed":
                sys.exit(f"ERROR: export failed: {body}")
            if time.monotonic() > deadline:
                sys.exit(f"ERROR: export not complete after {POLL_TIMEOUT_S}s")
            time.sleep(POLL_INTERVAL_S)

        download = client.get(url, follow_redirects=True)
        download.raise_for_status()
        return download.text


# ---------------------------------------------------------------------------
# DB fill — NULL-guarded, idempotent
# ---------------------------------------------------------------------------


async def fill_tables(
    team_id: str, records: list[StatsRecord], *, dry_run: bool
) -> dict:
    import asyncpg

    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn:
        sys.exit("ERROR: DATABASE_URL not set.")

    report = {
        "rows_in_export": len(records),
        "with_disposition": 0,
        "cc_calls_filled": 0,
        "evals_filled": 0,
        "row_failures": 0,
    }
    dispositioned = [r for r in records if r.disposition_category]
    report["with_disposition"] = len(dispositioned)

    conn = await asyncpg.connect(dsn)
    try:
        for record in dispositioned:
            try:
                if dry_run:
                    cc_hit = await conn.fetchval(
                        "SELECT COUNT(*) FROM command_center.calls "
                        "WHERE team_id = $1 AND dialpad_call_id = $2 "
                        "  AND disposition_source IS NULL",
                        team_id, record.call_id,
                    )
                    ev_hit = await conn.fetchval(
                        "SELECT COUNT(*) FROM qa.evaluations "
                        "WHERE team_id = $1 AND dialpad_disposition_category IS NULL "
                        "  AND (dialpad_call_id = $2 "
                        "       OR dialpad_entry_point_call_id = $2 "
                        "       OR dialpad_master_call_id = $2)",
                        team_id, record.call_id,
                    )
                    report["cc_calls_filled"] += cc_hit
                    report["evals_filled"] += ev_hit
                    continue

                cc_result = await conn.execute(
                    "UPDATE command_center.calls "
                    "SET disposition_category = $3, disposition = $4, "
                    "    disposition_source = 'stats_pull' "
                    "WHERE team_id = $1 AND dialpad_call_id = $2 "
                    "  AND disposition_source IS NULL",
                    team_id, record.call_id,
                    record.disposition_category, record.disposition,
                )
                ev_result = await conn.execute(
                    "UPDATE qa.evaluations "
                    "SET dialpad_disposition_category = $3, "
                    "    dialpad_disposition = $4 "
                    "WHERE team_id = $1 AND dialpad_disposition_category IS NULL "
                    "  AND (dialpad_call_id = $2 "
                    "       OR dialpad_entry_point_call_id = $2 "
                    "       OR dialpad_master_call_id = $2)",
                    team_id, record.call_id,
                    record.disposition_category, record.disposition,
                )
                report["cc_calls_filled"] += int(cc_result.split()[-1])
                report["evals_filled"] += int(ev_result.split()[-1])
            except Exception as e:  # noqa: BLE001 — overnight contract
                report["row_failures"] += 1
                print(f"  ROW FAILURE call_id={record.call_id}: {e}", file=sys.stderr)
    finally:
        await conn.close()
    return report


def print_report(team_id: str, report: dict, *, dry_run: bool) -> None:
    label = "DRY-RUN (would fill)" if dry_run else "filled"
    total = report["rows_in_export"]
    with_disp = report["with_disposition"]
    pct = (100 * with_disp / total) if total else 0.0
    print()
    print(f"=== pull_dispositions report — {team_id} ===")
    print(f"rows in export:        {total}")
    print(f"with disposition:      {with_disp} ({pct:.0f}% coverage)")
    print(f"cc.calls {label}:      {report['cc_calls_filled']}")
    print(f"qa.evaluations {label}: {report['evals_filled']}")
    print(f"row failures:          {report['row_failures']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--team-id", default="member_support")
    parser.add_argument("--days-start", type=int, default=30,
                        help="days ago the range STARTS (older bound)")
    parser.add_argument("--days-end", type=int, default=0,
                        help="days ago the range ENDS (newer bound)")
    parser.add_argument("--csv", default=None,
                        help="local export CSV — skip the Stats API")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.csv:
        text = Path(args.csv).read_text(encoding="utf-8")
        records = parse_export_csv(text)
    else:
        records = []
        for cc_id in team_call_center_ids(args.team_id):
            print(f"fetching Stats export for call center {cc_id} "
                  f"(days {args.days_start} → {args.days_end}) ...")
            records.extend(
                parse_export_csv(
                    fetch_export(cc_id, args.days_start, args.days_end)
                )
            )

    report = asyncio.run(
        fill_tables(args.team_id, records, dry_run=args.dry_run)
    )
    print_report(args.team_id, report, dry_run=args.dry_run)
    return 2 if report["row_failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

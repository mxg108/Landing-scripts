#!/usr/bin/env python3
"""Backfill `date_connected` into Analyst_History for pre-PR-1 rows.

PR-2 of the call-time initiative
(`references/CallTimeOnAnalystHistory.md`). PR-1 repurposed
`COL_TIMESTAMP` (col C) to hold the call's `date_connected` from
Dialpad and added a new trailing column `col_eval_approved_at` for
the eval-time UTC string that used to live in col C. New approvals
land in the new shape automatically; this script walks Analyst_History
and migrates rows that are still in the old shape:

  Old-shape (needs backfill):
    col C            = eval-time UTC string (legacy)
    col eval_approved_at = blank

  After backfill:
    col C            = call's date_connected (from Dialpad)
    col eval_approved_at = the original col C value (eval time)

Two-cell sheet update per row, rate-limited to ≤5 req/s (Dialpad's
documented cap). Resumable via `.backfill-log` (gitignored). Rows the
script can't backfill — Dialpad 404, blank date_connected, or
otherwise unresolvable — are logged to `.backfill-skipped.csv` for
manual triage; they survive cleanly under PR-1's `load_and_clean`
fallback (col C stays eval time on those rows).

ID-mismatch context (from the design doc, "Newly-raised risk"):
Most historical rows store `entry_point_call_id` in `dialpad_link`,
not the master `call_id`. Dialpad's `/api/v2/call/{id}` endpoint is
documented as master-keyed; for direct calls entry_point == master
and lookup succeeds, for queue calls they diverge. V1 of this script
attempts `get_call_details(eval_id)` directly and accepts the
resulting skip rate; entry_point → master reverse-lookup via
`list_calls_for_user` is a separate enhancement if the skip rate
proves too high in practice.

Usage:
    cd qa-automation/AI-Scoring
    python3 scripts/backfill_call_started.py --team-id member_support
    python3 scripts/backfill_call_started.py --team-id sales --dry-run
    python3 scripts/backfill_call_started.py --team-id sales --max-rows 100

Flags:
    --team-id <id>    (required) Team config to load (e.g. sales,
                      member_support). Drives the sheet ID env var
                      and the history layout.
    --dry-run         Print planned mutations; do not write.
    --max-rows N      Cap mutations per run (default: unlimited).
                      Useful for incremental rollout.
    --resume          Skip rows previously recorded in .backfill-log.
                      Default is True; pass --no-resume to force
                      re-processing.

Output files (gitignored):
    .backfill-log               One line per successfully migrated row:
                                  YYYY-MM-DDTHH:MM:SSZ\\tteam_id\\trow_num\\teval_id
    .backfill-skipped.csv       CSV of rows we couldn't migrate:
                                  row_num,agent,eval_id,reason
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials

# Load .env from AI-Scoring directory regardless of where the script is launched.
_AI_SCORING = Path(__file__).resolve().parent.parent
_env_path = _AI_SCORING / ".env"
load_dotenv(_env_path)

# Make `backend.*` importable so we can reuse the production config + clients.
if str(_AI_SCORING) not in sys.path:
    sys.path.insert(0, str(_AI_SCORING))

from backend.config import history_layout
from backend.config.env import env_for_team
from backend.config.team_config import get_team_config
from backend.services.dialpad_client import _epoch_ms_to_utc_datetime, get_call_details


SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Output files live at the AI-Scoring root (one level up from /scripts).
BACKFILL_LOG = _AI_SCORING / ".backfill-log"
SKIPPED_CSV = _AI_SCORING / ".backfill-skipped.csv"

# Dialpad's documented cap is 5 req/s; we sleep slightly more to leave
# headroom for retries and concurrent traffic from the live scoring app
# (the production semaphore already throttles its own calls, but we run
# out-of-band).
RATE_LIMIT_SECONDS = 0.25  # → ~4 req/s


def _format_call_time(dt: datetime | None) -> str:
    """Same canonical format as sheets_service._format_call_started.

    Kept inline here so the backfill stays self-contained — the sheets
    service helper depends on the writer's full import graph; this
    script only needs the format string.
    """
    if dt is None:
        return ""
    return dt.strftime("%m/%d/%Y %H:%M:%S")


def _eval_id_from_link(link: str) -> str:
    """Mirror of services/team_stats.py:_parse_row eval_id parsing.

    Returns the trailing path segment of a Dialpad link, stripping
    [LONG CALL] suffixes and query strings. May be the
    entry_point_call_id or the master call_id depending on when the
    row was scored (see script docstring).
    """
    if not link:
        return ""
    clean = link.split("[")[0].strip().split("?")[0].strip()
    return clean.rstrip("/").split("/")[-1]


def _load_resume_set() -> set[tuple[str, int]]:
    """Return {(team_id, row_num)} previously logged as migrated.

    Lets a partial run resume without re-touching rows we already
    wrote. The log is tab-separated to stay greppable / append-only.
    """
    if not BACKFILL_LOG.exists():
        return set()
    seen: set[tuple[str, int]] = set()
    with BACKFILL_LOG.open("r", encoding="utf-8") as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 3:
                continue
            try:
                seen.add((parts[1], int(parts[2])))
            except ValueError:
                continue
    return seen


def _append_resume_entry(team_id: str, row_num: int, eval_id: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with BACKFILL_LOG.open("a", encoding="utf-8") as f:
        f.write(f"{ts}\t{team_id}\t{row_num}\t{eval_id}\n")


def _append_skipped(row_num: int, agent: str, eval_id: str, reason: str) -> None:
    """Append to .backfill-skipped.csv; create with header if missing."""
    write_header = not SKIPPED_CSV.exists()
    with SKIPPED_CSV.open("a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if write_header:
            w.writerow(["row_num", "agent", "eval_id", "reason"])
        w.writerow([row_num, agent, eval_id, reason])


def _gspread_client() -> gspread.Client:
    creds_env = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not creds_env:
        print("ERROR: GOOGLE_SERVICE_ACCOUNT_JSON must be set in .env", file=sys.stderr)
        sys.exit(1)
    if creds_env.strip().startswith("{"):
        creds = Credentials.from_service_account_info(json.loads(creds_env), scopes=SCOPES)
    else:
        creds = Credentials.from_service_account_file(creds_env, scopes=SCOPES)
    client = gspread.authorize(creds)
    client.set_timeout(120)
    return client


def _col_letter(idx: int) -> str:
    """0-based column index → spreadsheet letter. Avoids importing
    backend.config.history_layout.col_index_to_letter to keep this
    script's import graph thin."""
    letters = ""
    n = idx
    while True:
        letters = chr(ord("A") + n % 26) + letters
        n = n // 26 - 1
        if n < 0:
            break
    return letters


async def _backfill_one_row(
    *,
    row_num: int,
    agent: str,
    eval_id: str,
    old_col_c: str,
    new_col_idx: int,
    dry_run: bool,
    sheet: gspread.Worksheet,
) -> tuple[str, str | None]:
    """Look up date_connected for one row and apply the two-cell update.

    Returns (status, reason) where status ∈ {"migrated", "skipped"} and
    reason is populated for skips. The caller appends to .backfill-log
    on migrated and to .backfill-skipped.csv on skipped.
    """
    if not eval_id:
        return ("skipped", "no_eval_id")

    try:
        details = await get_call_details(eval_id)
    except Exception as e:
        return ("skipped", f"dialpad_error:{type(e).__name__}")

    dc_raw = details.get("date_connected", "")
    dt = _epoch_ms_to_utc_datetime(dc_raw)
    if dt is None:
        # Either Dialpad 404'd (empty defaults) or the call truly has no
        # date_connected. Both reasons collapse here; manual triage via
        # the skipped CSV can disambiguate using the row number.
        return ("skipped", "no_date_connected")

    new_col_c = _format_call_time(dt)
    col_c_letter = _col_letter(history_layout.COL_TIMESTAMP)
    new_col_letter = _col_letter(new_col_idx)

    if dry_run:
        print(
            f"  [dry-run] row {row_num} ({agent}, eval_id={eval_id}): "
            f"col {col_c_letter}: {old_col_c!r} → {new_col_c!r}; "
            f"col {new_col_letter}: '' → {old_col_c!r}"
        )
        return ("migrated", None)

    # Two-cell update on the same row — single batch call so the row
    # never sits in a partially-migrated state.
    sheet.batch_update([
        {"range": f"{col_c_letter}{row_num}", "values": [[new_col_c]]},
        {"range": f"{new_col_letter}{row_num}", "values": [[old_col_c]]},
    ], value_input_option="USER_ENTERED")
    return ("migrated", None)


async def main_async() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--team-id", required=True, help="e.g. member_support, sales")
    p.add_argument("--dry-run", action="store_true", help="Print planned mutations; do not write.")
    p.add_argument("--max-rows", type=int, default=None,
                   help="Cap migrations per run. Default: no cap.")
    p.add_argument("--no-resume", action="store_true",
                   help="Re-process rows already in .backfill-log.")
    args = p.parse_args()

    team_id = args.team_id
    config = get_team_config(team_id)
    L = config.history_layout

    sheet_id = env_for_team("GOOGLE_SHEETS_ID", team_id, legacy_ok=True)
    if not sheet_id:
        print(f"ERROR: GOOGLE_SHEETS_ID (or GOOGLE_SHEETS_ID_{team_id.upper()}) not set", file=sys.stderr)
        return 1
    tab_name = (
        env_for_team("GOOGLE_HISTORY_TAB", team_id, legacy_ok=True)
        or config.sheets.analyst_history.tab_name
    )

    print(f"Team: {team_id}")
    print(f"Sheet tab: {tab_name}")
    print(f"col_eval_approved_at index: {L.col_eval_approved_at} (col {_col_letter(L.col_eval_approved_at)})")
    print(f"Dry-run: {args.dry_run}")
    print(f"Rate limit: {RATE_LIMIT_SECONDS}s between Dialpad calls")
    print()

    gc = _gspread_client()
    sheet = gc.open_by_key(sheet_id).worksheet(tab_name)

    # Ensure the grid is wide enough for the new trailing column.
    # PR-1 bumped TRAILING_WIDTH 6 → 7, so existing sheets provisioned at
    # the old width (e.g. sales N=19 at 69 cols) will 400 on any write to
    # col BR. gspread's batch_update doesn't auto-grow the grid; we have
    # to expand it explicitly once. Idempotent — add_cols is a no-op
    # when col_count already meets total_width.
    if sheet.col_count < L.total_width:
        missing = L.total_width - sheet.col_count
        if args.dry_run:
            print(
                f"[dry-run] would expand grid: {sheet.col_count} → "
                f"{L.total_width} cols (add_cols({missing}))"
            )
        else:
            print(
                f"Expanding grid: {sheet.col_count} → {L.total_width} cols "
                f"(add_cols({missing}))"
            )
            sheet.add_cols(missing)

    all_rows = sheet.get_all_values()
    print(f"Loaded {len(all_rows) - 1} data rows from {tab_name}.")

    resume_set = set() if args.no_resume else _load_resume_set()
    if resume_set:
        print(f"Resume set: {sum(1 for t, _ in resume_set if t == team_id)} rows previously migrated for {team_id}.")

    # Identify candidate rows. We process newest-first so analytics-relevant
    # months get backfilled before deep history. row_num is 1-indexed +
    # accounts for the header row (sheet row 1 = header → data starts at 2).
    candidates: list[tuple[int, list[str]]] = []
    for i, row in enumerate(all_rows[1:], start=2):
        if not row:
            continue
        # Skip rows already in new-shape (col_eval_approved_at populated).
        new_col_val = row[L.col_eval_approved_at] if len(row) > L.col_eval_approved_at else ""
        if new_col_val.strip():
            continue
        # Skip rows we've already migrated (resume support).
        if (team_id, i) in resume_set:
            continue
        candidates.append((i, row))

    # Newest-first: sort by col C parsed-ish (newest dates last in sheet
    # convention; we want them first to process). We don't strictly parse
    # here — sheet append order is monotonic so reverse-index ordering
    # is a cheap proxy.
    candidates.reverse()

    if args.max_rows is not None:
        candidates = candidates[: args.max_rows]

    print(f"Backfill candidates: {len(candidates)}")
    print()

    migrated = 0
    skipped = 0
    for row_num, row in candidates:
        agent = str(row[history_layout.COL_AGENT_NAME]).strip()
        old_col_c = row[history_layout.COL_TIMESTAMP] if len(row) > history_layout.COL_TIMESTAMP else ""
        dialpad_link = row[history_layout.COL_DIALPAD_LINK] if len(row) > history_layout.COL_DIALPAD_LINK else ""
        eval_id = _eval_id_from_link(dialpad_link)

        status, reason = await _backfill_one_row(
            row_num=row_num,
            agent=agent,
            eval_id=eval_id,
            old_col_c=old_col_c,
            new_col_idx=L.col_eval_approved_at,
            dry_run=args.dry_run,
            sheet=sheet,
        )
        if status == "migrated":
            migrated += 1
            if not args.dry_run:
                _append_resume_entry(team_id, row_num, eval_id)
        else:
            skipped += 1
            if not args.dry_run:
                _append_skipped(row_num, agent, eval_id, reason or "unknown")
            print(f"  [skip] row {row_num} ({agent}, eval_id={eval_id}): {reason}")

        # Pace between Dialpad calls. Even on skip we may have made an
        # HTTP request, so sleep regardless.
        await asyncio.sleep(RATE_LIMIT_SECONDS)

    print()
    print(f"Summary for {team_id}:")
    print(f"  Migrated: {migrated}")
    print(f"  Skipped:  {skipped}")
    if args.dry_run:
        print(f"  (Dry-run — no writes performed.)")
    else:
        print(f"  Resume log: {BACKFILL_LOG}")
        if skipped:
            print(f"  Skipped CSV: {SKIPPED_CSV}")
    return 0


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    sys.exit(main())

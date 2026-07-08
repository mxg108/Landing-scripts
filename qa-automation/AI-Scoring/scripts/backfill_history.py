#!/usr/bin/env python3
"""One-time backfill: copies Dialpad Link, Key Strengths, and
Opportunities from a legacy form-responses tab into Analyst_History
rows that are missing them.

Matches rows by eval timestamp + agent name between the two tabs.
Column positions on the Analyst_History side are derived from the
team's config (`HistoryLayout`) — never hardcoded, so the script is
safe against the post-Phase-2 43/70-column layouts and any future
section-count change.

Timestamp note (call-time initiative): the eval/approval time this
script matches on lives in the trailing `eval_approved_at` column on
new-shape rows, falling back to col C on rows the PR-2
`backfill_call_started.py` migration hasn't touched yet — the same
transition semantics as `load_and_clean`.

Usage:
    cd qa-automation/AI-Scoring
    python3 scripts/backfill_history.py --team-id member_support [--dry-run]

Flags:
    --team-id <id>      (required) Team config to load (e.g. sales,
                        member_support). Drives the sheet ID env var
                        and the history layout.
    --source-tab NAME   Form-responses tab to copy from
                        (default: "Form Responses 1").
    --dry-run           Print what would be updated without writing.
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials

# Load .env from AI-Scoring directory regardless of where the script is launched.
_AI_SCORING = Path(__file__).resolve().parent.parent
load_dotenv(_AI_SCORING / ".env")

# Make `backend.*` importable so we reuse the production config.
if str(_AI_SCORING) not in sys.path:
    sys.path.insert(0, str(_AI_SCORING))

from backend.config import history_layout
from backend.config.history_layout import col_index_to_letter
from backend.config.env import env_for_team
from backend.config.team_config import get_team_config

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

# Form-responses source column indices (0-based). The legacy form tab is
# frozen — no new rows land there — so these stay literal.
F1_TIMESTAMP = 0
F1_AGENT_NAME = 2
F1_KEY_STRENGTHS = 13   # col N
F1_OPPORTUNITIES = 14   # col O
F1_DIALPAD_LINK = 15    # col P

# Timestamp formats to try
TS_FORMATS = [
    "%m/%d/%Y %H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%m/%d/%Y %H:%M",
    "%Y-%m-%dT%H:%M:%S",
]


def parse_ts(val: str) -> str:
    """Normalize a timestamp string to a comparable format."""
    val = str(val).strip()
    for fmt in TS_FORMATS:
        try:
            dt = datetime.strptime(val, fmt)
            return dt.strftime("%Y-%m-%d %H:%M")  # normalize to minute
        except ValueError:
            continue
    return val  # return raw if no format matches


def safe(row, idx):
    """Get row[idx] safely."""
    return str(row[idx]).strip() if idx < len(row) else ""


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--team-id", required=True,
                        help="Team config to load (e.g. sales, member_support)")
    parser.add_argument("--source-tab", default="Form Responses 1",
                        help='Form-responses tab to copy from (default: "Form Responses 1")')
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be updated without writing to Sheets")
    args = parser.parse_args()

    config = get_team_config(args.team_id)
    layout = config.history_layout

    # Analyst_History columns, derived from the team's section count.
    ah_dialpad = history_layout.COL_DIALPAD_LINK
    ah_strengths = layout.col_key_strengths
    ah_opportunities = layout.col_opportunities

    dialpad_letter = col_index_to_letter(ah_dialpad)
    strengths_letter = col_index_to_letter(ah_strengths)
    opportunities_letter = col_index_to_letter(ah_opportunities)

    # Connect to Sheets
    creds_env = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    sheet_id = env_for_team("GOOGLE_SHEETS_ID", args.team_id, legacy_ok=True)

    if not creds_env or not sheet_id:
        print("ERROR: GOOGLE_SERVICE_ACCOUNT_JSON and GOOGLE_SHEETS_ID"
              f" (or GOOGLE_SHEETS_ID_{args.team_id.upper()}) must be set in .env")
        sys.exit(1)

    if creds_env.strip().startswith("{"):
        creds = Credentials.from_service_account_info(json.loads(creds_env), scopes=SCOPES)
    else:
        creds = Credentials.from_service_account_file(creds_env, scopes=SCOPES)

    client = gspread.authorize(creds)
    client.set_timeout(120)
    spreadsheet = client.open_by_key(sheet_id)

    print(f"Team: {args.team_id} (N={layout.n} sections, "
          f"dialpad={dialpad_letter}, strengths={strengths_letter}, "
          f"opportunities={opportunities_letter})")

    print(f"Reading {args.source_tab}...")
    form1 = spreadsheet.worksheet(args.source_tab)
    form1_data = form1.get_all_values()
    print(f"  {len(form1_data) - 1} data rows")

    history_tab = config.sheets.analyst_history.tab_name
    print(f"Reading {history_tab}...")
    history = spreadsheet.worksheet(history_tab)
    history_data = history.get_all_values()
    print(f"  {len(history_data) - 1} data rows")

    # Build lookup from the source tab: (normalized_ts, agent_lower) -> row
    form1_lookup = {}
    for row in form1_data[1:]:  # skip header
        ts = parse_ts(safe(row, F1_TIMESTAMP))
        agent = safe(row, F1_AGENT_NAME).lower()
        form1_lookup[(ts, agent)] = row

    print(f"  Source lookup: {len(form1_lookup)} unique (timestamp, agent) pairs")

    # Scan Analyst_History for rows missing Dialpad Link, Key Strengths,
    # or Opportunities
    updates = []
    matched = 0
    skipped_no_match = 0
    skipped_already_filled = 0

    for i, row in enumerate(history_data[1:], start=2):  # skip header, 1-indexed in sheet
        # Eval time: new-shape rows carry it in eval_approved_at; pre-PR-2
        # rows still have it in col C (see module docstring).
        raw_ts = safe(row, layout.col_eval_approved_at) or safe(row, history_layout.COL_TIMESTAMP)
        ts = parse_ts(raw_ts)
        agent = safe(row, history_layout.COL_AGENT_NAME).lower()

        if not ts or not agent:
            continue

        form1_row = form1_lookup.get((ts, agent))
        if not form1_row:
            skipped_no_match += 1
            continue

        matched += 1

        current_link = safe(row, ah_dialpad)
        current_strengths = safe(row, ah_strengths)
        current_opportunities = safe(row, ah_opportunities)

        new_link = safe(form1_row, F1_DIALPAD_LINK)
        new_strengths = safe(form1_row, F1_KEY_STRENGTHS)
        new_opportunities = safe(form1_row, F1_OPPORTUNITIES)

        row_updates = {}
        if not current_link and new_link:
            row_updates["dialpad_link"] = new_link
        if not current_strengths and new_strengths:
            row_updates["key_strengths"] = new_strengths
        if not current_opportunities and new_opportunities:
            row_updates["opportunities"] = new_opportunities

        if row_updates:
            updates.append({
                "sheet_row": i,
                "agent": safe(row, history_layout.COL_AGENT_NAME),
                "timestamp": ts,
                "updates": row_updates,
            })
        else:
            skipped_already_filled += 1

    # Report
    print(f"\nResults:")
    print(f"  Matched:         {matched}")
    print(f"  No match:        {skipped_no_match}")
    print(f"  Already filled:  {skipped_already_filled}")
    print(f"  To update:       {len(updates)}")

    if not updates:
        print("\nNothing to update.")
        return

    # Show preview
    print(f"\nPreview (first 10):")
    for u in updates[:10]:
        cols = ", ".join(f"{k}={v[:40]}..." if len(v) > 40 else f"{k}={v}"
                         for k, v in u["updates"].items())
        print(f"  Row {u['sheet_row']}: {u['agent']} ({u['timestamp']}) -> {cols}")

    if len(updates) > 10:
        print(f"  ... and {len(updates) - 10} more")

    if args.dry_run:
        print("\n[DRY RUN] No changes written. Remove --dry-run to apply.")
        return

    # Apply updates. The dialpad-link cell sits in the fixed prefix while
    # strengths/opportunities are trailing columns, so each row needs two
    # ranges — batched into a single API call per row, rate-limited to
    # stay under the 60 writes/min quota.
    print(f"\nWriting {len(updates)} row updates to {history_tab}...")
    print(f"  Rate limit: 50 writes/min (~1.2s between writes)")

    written = 0
    errors = 0
    start_time = time.time()

    for idx, u in enumerate(updates):
        row_num = u["sheet_row"]
        cols = u["updates"]

        # Always write all three cells, keeping existing values where no
        # update applies — same batching strategy as the original script.
        row_data = history_data[row_num - 1]  # 0-indexed
        link_val = cols.get("dialpad_link", safe(row_data, ah_dialpad))
        strengths_val = cols.get("key_strengths", safe(row_data, ah_strengths))
        opportunities_val = cols.get("opportunities", safe(row_data, ah_opportunities))

        batch = [
            {"range": f"{dialpad_letter}{row_num}", "values": [[link_val]]},
            {"range": f"{strengths_letter}{row_num}:{opportunities_letter}{row_num}",
             "values": [[strengths_val, opportunities_val]]},
        ]

        retries = 0
        while retries < 3:
            try:
                history.batch_update(batch, value_input_option="USER_ENTERED")
                written += 1
                break
            except Exception as e:
                if "429" in str(e) or "Quota exceeded" in str(e):
                    retries += 1
                    wait = 30 * retries
                    print(f"  Rate limited at row {row_num}, waiting {wait}s (retry {retries}/3)...")
                    time.sleep(wait)
                else:
                    print(f"  ERROR at row {row_num}: {e}")
                    errors += 1
                    break

        # Rate limiter: ~1.2 seconds between writes (50/min)
        time.sleep(1.2)

        # Progress every 50 rows
        if (idx + 1) % 50 == 0:
            elapsed = time.time() - start_time
            rate = (idx + 1) / elapsed * 60
            remaining = (len(updates) - idx - 1) / rate * 60 if rate > 0 else 0
            print(f"  Progress: {idx + 1}/{len(updates)} ({rate:.0f} rows/min, ~{remaining:.0f}s remaining)")

    elapsed = time.time() - start_time
    print(f"\nDone. {written} rows updated, {errors} errors. Took {elapsed:.0f}s.")


if __name__ == "__main__":
    main()

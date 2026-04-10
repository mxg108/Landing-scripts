#!/usr/bin/env python3
"""
One-time backfill script: copies Dialpad Link, Key Strengths, and
Opportunities from Form Responses 1 into Analyst_History rows that
are missing them.

Matches rows by timestamp + agent name between the two sheets.

Usage:
    cd qa-automation/AI-Scoring
    python3 scripts/backfill_history.py [--dry-run]

Flags:
    --dry-run   Print what would be updated without writing to Sheets.
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials

# Load .env from AI-Scoring directory
_env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_env_path)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

# Form Responses 1 column indices (0-based)
F1_TIMESTAMP = 0
F1_AGENT_NAME = 2
F1_KEY_STRENGTHS = 13   # col N
F1_OPPORTUNITIES = 14   # col O
F1_DIALPAD_LINK = 15    # col P

# Analyst_History column indices (0-based)
AH_AGENT_NAME = 0
AH_TIMESTAMP = 2
AH_DIALPAD_LINK = 15    # col P
AH_KEY_STRENGTHS = 16   # col Q
AH_IMPROVEMENTS = 17    # col R

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
    dry_run = "--dry-run" in sys.argv

    # Connect to Sheets
    creds_env = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    sheet_id = os.getenv("GOOGLE_SHEETS_ID", "")

    if not creds_env or not sheet_id:
        print("ERROR: GOOGLE_SERVICE_ACCOUNT_JSON and GOOGLE_SHEETS_ID must be set in .env")
        sys.exit(1)

    if creds_env.strip().startswith("{"):
        creds = Credentials.from_service_account_info(json.loads(creds_env), scopes=SCOPES)
    else:
        creds = Credentials.from_service_account_file(creds_env, scopes=SCOPES)

    client = gspread.authorize(creds)
    client.set_timeout(120)
    spreadsheet = client.open_by_key(sheet_id)

    print("Reading Form Responses 1...")
    form1 = spreadsheet.worksheet("Form Responses 1")
    form1_data = form1.get_all_values()
    print(f"  {len(form1_data) - 1} data rows")

    print("Reading Analyst_History...")
    history = spreadsheet.worksheet("Analyst_History")
    history_data = history.get_all_values()
    print(f"  {len(history_data) - 1} data rows")

    # Build lookup from Form Responses 1: (normalized_ts, agent_lower) -> row
    form1_lookup = {}
    for i, row in enumerate(form1_data[1:], start=2):  # skip header, 1-indexed
        ts = parse_ts(safe(row, F1_TIMESTAMP))
        agent = safe(row, F1_AGENT_NAME).lower()
        key = (ts, agent)
        form1_lookup[key] = row

    print(f"  Form 1 lookup: {len(form1_lookup)} unique (timestamp, agent) pairs")

    # Scan Analyst_History for rows missing Dialpad Link, Key Strengths, or Improvements
    updates = []
    matched = 0
    skipped_no_match = 0
    skipped_already_filled = 0

    for i, row in enumerate(history_data[1:], start=2):  # skip header, 1-indexed in sheet
        ts = parse_ts(safe(row, AH_TIMESTAMP))
        agent = safe(row, AH_AGENT_NAME).lower()

        if not ts or not agent:
            continue

        key = (ts, agent)
        form1_row = form1_lookup.get(key)

        if not form1_row:
            skipped_no_match += 1
            continue

        matched += 1

        # Check what's missing in Analyst_History
        current_link = safe(row, AH_DIALPAD_LINK)
        current_strengths = safe(row, AH_KEY_STRENGTHS)
        current_improvements = safe(row, AH_IMPROVEMENTS)

        new_link = safe(form1_row, F1_DIALPAD_LINK)
        new_strengths = safe(form1_row, F1_KEY_STRENGTHS)
        new_opportunities = safe(form1_row, F1_OPPORTUNITIES)

        row_updates = {}
        if not current_link and new_link:
            row_updates["P"] = new_link
        if not current_strengths and new_strengths:
            row_updates["Q"] = new_strengths
        if not current_improvements and new_opportunities:
            row_updates["R"] = new_opportunities

        if row_updates:
            updates.append({
                "sheet_row": i,
                "agent": safe(row, AH_AGENT_NAME),
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
        cols = ", ".join(f"{k}={v[:40]}..." if len(v) > 40 else f"{k}={v}" for k, v in u["updates"].items())
        print(f"  Row {u['sheet_row']}: {u['agent']} ({u['timestamp']}) -> {cols}")

    if len(updates) > 10:
        print(f"  ... and {len(updates) - 10} more")

    if dry_run:
        print("\n[DRY RUN] No changes written. Remove --dry-run to apply.")
        return

    # Apply updates — batch P/Q/R into a single row update per row,
    # with rate limiting (max 50 requests/min to stay under 60/min quota)
    import time

    print(f"\nWriting {len(updates)} row updates to Analyst_History...")
    print(f"  Rate limit: 50 writes/min (~1.2s between writes)")

    written = 0
    errors = 0
    start_time = time.time()

    for idx, u in enumerate(updates):
        row_num = u["sheet_row"]
        cols = u["updates"]

        # Build a single batch: always write P, Q, R (use existing value if no update)
        row_data = history_data[row_num - 1]  # 0-indexed
        p_val = cols.get("P", safe(row_data, AH_DIALPAD_LINK))
        q_val = cols.get("Q", safe(row_data, AH_KEY_STRENGTHS))
        r_val = cols.get("R", safe(row_data, AH_IMPROVEMENTS))

        retries = 0
        while retries < 3:
            try:
                # Single API call writes all 3 columns at once (P=16, Q=17, R=18 → 1-indexed)
                history.update([[p_val, q_val, r_val]], f"P{row_num}:R{row_num}",
                               value_input_option="USER_ENTERED")
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

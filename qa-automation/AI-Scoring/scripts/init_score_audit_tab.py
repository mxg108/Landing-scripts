"""
init_score_audit_tab.py — idempotently create the Score_Audit tab.

Usage:
    python3 scripts/init_score_audit_tab.py [--dry-run]

Run this ONCE per environment (local dev + each Railway deploy that has its
own spreadsheet). Reads ``GOOGLE_SERVICE_ACCOUNT_JSON`` + ``GOOGLE_SHEETS_ID``
(or ``GOOGLE_SHEETS_ID_MEMBER_SUPPORT``) from the env and:

  * No-op if the Score_Audit tab already exists (just reports the row count).
  * Otherwise creates the tab and writes the canonical header row.

Safe to re-run — checks for existing tabs by name before creating.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Make backend imports resolve when running from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.config import score_audit as audit_cfg  # noqa: E402
from backend.services.sheets_service import _get_spreadsheet  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would happen, but don't create or write anything.",
    )
    args = parser.parse_args()

    spreadsheet = _get_spreadsheet(audit_cfg.HOST_TEAM_ID)
    existing = {ws.title for ws in spreadsheet.worksheets()}

    if audit_cfg.TAB_NAME in existing:
        ws = spreadsheet.worksheet(audit_cfg.TAB_NAME)
        rows = ws.row_count
        print(
            f"Tab '{audit_cfg.TAB_NAME}' already exists on host '{audit_cfg.HOST_TEAM_ID}' "
            f"({rows} rows). No-op."
        )
        return 0

    print(
        f"Tab '{audit_cfg.TAB_NAME}' missing on host '{audit_cfg.HOST_TEAM_ID}'. "
        f"Will create with {len(audit_cfg.COLUMNS)} columns: {audit_cfg.COLUMNS}"
    )
    if args.dry_run:
        print("Dry run — nothing written.")
        return 0

    ws = spreadsheet.add_worksheet(
        title=audit_cfg.TAB_NAME,
        rows=1000,
        cols=len(audit_cfg.COLUMNS),
    )
    ws.update("A1", [audit_cfg.COLUMNS], value_input_option="USER_ENTERED")
    print(f"Created '{audit_cfg.TAB_NAME}' and wrote header row.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

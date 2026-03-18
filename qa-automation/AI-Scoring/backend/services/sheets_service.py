"""
Google Sheets writer.
Appends a draft QA scorecard row to the existing QA Google Sheet.

Column layout (fixed — must match existing sheet):
  A: Timestamp
  B: Manager Email Address
  C: Agent being evaluated
  D: Greeting
  E: Caller Identity Validation
  F: Purpose of the Call
  G: Matching the Moment
  H: Process Adherence
  I: Call Resolution
  J: Communication
  K: Efficiency & Call Handling
  L: Documentation
  M: Customer Resolution Indicator
  N: Key Strengths
  O: Opportunities for Improvement
  P: Dialpad Link

Setup:
  1. Create a Google Cloud service account
  2. Share the QA sheet with the service account email (Editor access)
  3. Download the JSON credentials file
  4. Set GOOGLE_SERVICE_ACCOUNT_JSON=/path/to/credentials.json in .env
  5. Set GOOGLE_SHEETS_ID=<spreadsheet_id> in .env
  6. Set GOOGLE_SHEETS_TAB=<tab name> in .env (default: "QA Scores")
"""

import json
import os
from datetime import datetime, timezone

import gspread
from google.oauth2.service_account import Credentials

from backend.models.scorecard import ScorecardWithMeta

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Section id → column letter mapping (D through M)
SECTION_COLUMN_ORDER = [
    "greeting",
    "caller_identity_validation",
    "purpose_of_call",
    "matching_the_moment",
    "process_adherence",
    "call_resolution",
    "communication",
    "efficiency_call_handling",
    # documentation is column L — always manual
    "customer_resolution_indicator",
]


def _get_sheet():
    creds_path = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    sheet_id = os.getenv("GOOGLE_SHEETS_ID")
    tab_name = os.getenv("GOOGLE_SHEETS_TAB", "QA Scores")

    if not creds_path or not sheet_id:
        raise RuntimeError(
            "GOOGLE_SERVICE_ACCOUNT_JSON and GOOGLE_SHEETS_ID must be set in .env"
        )

    creds = Credentials.from_service_account_file(creds_path, scopes=SCOPES)
    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(sheet_id)
    return spreadsheet.worksheet(tab_name)


YN_DISPLAY = {
    "Y": "Yes",
    "N": "No",
    "NA": "Not Applicable",
}


def _format_score(section: dict) -> str:
    """Return display value for a section score: int, 'Yes', 'No', or 'Not Applicable'."""
    if section["score_type"] == "yn":
        yn = section.get("yn_value") or "NA"
        return YN_DISPLAY.get(yn, "Not Applicable")
    score = section.get("score")
    return str(score) if score is not None else "N/A"


def append_scorecard_row(scorecard: ScorecardWithMeta) -> int:
    """
    Append one draft row to the QA Google Sheet.
    Returns the row number written, or -1 if Sheets is not configured.
    Documentation column (L) is always 'Pending manual review'.
    """
    if not os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON") or not os.getenv("GOOGLE_SHEETS_ID"):
        print("[sheets_service] Sheets not configured — skipping write.")
        return -1

    sheet = _get_sheet()

    # Build section lookup by id
    sections_by_id = {s.id: s.model_dump() for s in scorecard.sections}

    # Columns D–K (sections 1–8)
    scored_columns = []
    for section_id in SECTION_COLUMN_ORDER[:8]:  # D through K
        section = sections_by_id.get(section_id, {})
        scored_columns.append(_format_score(section))

    # Column L — Documentation (always manual, pass 1 as placeholder)
    documentation = 1

    # Column M — Customer Resolution Indicator (section 10)
    cri = sections_by_id.get("customer_resolution_indicator", {})
    cri_value = _format_score(cri)

    # Dialpad link — append long-call flag if applicable
    dialpad_link = scorecard.dialpad_link or ""
    if scorecard.flagged_long_call:
        dialpad_link += " [LONG CALL — Manager review recommended]"

    row = [
        datetime.now(timezone.utc).strftime("%m/%d/%Y %H:%M:%S"),  # A: Timestamp
        scorecard.manager_email or "",                                   # B: Manager Email
        scorecard.agent_name or "",                                      # C: Agent
        *scored_columns,                                                 # D–K: sections 1–8
        documentation,                                                   # L: Documentation
        cri_value,                                                       # M: CRI
        scorecard.key_strengths or "",                                   # N: Key Strengths
        scorecard.opportunities or "",                                   # O: Opportunities
        dialpad_link,                                                    # P: Dialpad Link
    ]

    result = sheet.append_row(row, value_input_option="USER_ENTERED")
    # gspread returns the update response; extract row number from range
    updated_range = result.get("updates", {}).get("updatedRange", "")
    try:
        row_num = int(updated_range.split("!")[1].split(":")[0][1:])
    except (IndexError, ValueError):
        row_num = -1

    return row_num

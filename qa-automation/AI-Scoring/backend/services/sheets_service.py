"""
Google Sheets writer.
Appends a draft QA scorecard row to the existing QA Google Sheet.

Column layout is defined in team config JSON — see
backend/config/teams/{team_id}.json for the mapping.

Setup:
  1. Create a Google Cloud service account
  2. Share the QA sheet with the service account email (Editor access)
  3. Download the JSON credentials file
  4. Set GOOGLE_SERVICE_ACCOUNT_JSON=/path/to/credentials.json in .env
  5. Set GOOGLE_SHEETS_ID=<spreadsheet_id> in .env
  6. Set GOOGLE_SHEETS_TAB=<tab name> in .env (default: "QA Scores")
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import gspread
from google.oauth2.service_account import Credentials

from backend.models.scorecard import ScorecardWithMeta

if TYPE_CHECKING:
    from backend.config.team_config import TeamConfig

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def _get_spreadsheet():
    """Return a fresh gspread Spreadsheet object."""
    creds_env = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    sheet_id = os.getenv("GOOGLE_SHEETS_ID")

    if not creds_env or not sheet_id:
        raise RuntimeError(
            "GOOGLE_SERVICE_ACCOUNT_JSON and GOOGLE_SHEETS_ID must be set"
        )

    if creds_env.strip().startswith("{"):
        creds_info = json.loads(creds_env)
        creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
    else:
        creds = Credentials.from_service_account_file(creds_env, scopes=SCOPES)

    client = gspread.authorize(creds)
    client.set_timeout(120)
    return client.open_by_key(sheet_id)


def _get_sheet(tab_name: str | None = None):
    """Return a worksheet from the QA spreadsheet."""
    tab = tab_name or os.getenv("GOOGLE_SHEETS_TAB", "QA Scores")
    return _get_spreadsheet().worksheet(tab)


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


def append_scorecard_row(scorecard: ScorecardWithMeta, config: TeamConfig) -> int:
    """
    Append one draft row to the QA Google Sheet.
    Returns the row number written, or -1 if Sheets is not configured.
    """
    if not os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON") or not os.getenv("GOOGLE_SHEETS_ID"):
        print("[sheets_service] Sheets not configured — skipping write.")
        return -1

    fai = config.sheets.form_responses_ai
    sheet = _get_sheet()

    # Build section lookup by id
    sections_by_id = {s.id: s.model_dump() for s in scorecard.sections}

    # Dialpad link — append long-call flag if applicable
    dialpad_link = scorecard.dialpad_link or ""
    if scorecard.flagged_long_call:
        dialpad_link += " [LONG CALL — Manager review recommended]"

    # Build row in explicit column order A–P from config column_map
    row = [
        datetime.now(timezone.utc).strftime("%m/%d/%Y %H:%M:%S"),   # A: Timestamp
        scorecard.manager_email or "",                                # B: Manager Email
        scorecard.agent_name or "",                                   # C: Agent
    ]

    # Score columns D through M (order from scored_section_columns)
    for col_letter in sorted(fai.scored_section_columns.keys()):
        section_id = fai.scored_section_columns[col_letter]
        row.append(_format_score(sections_by_id.get(section_id, {})))

    # Insert Documentation placeholder at the right position
    # Documentation is in column_map but NOT in scored_section_columns
    # It sits between the last numeric score column (K) and CRI (M)
    doc_col = None
    for letter, field_id in fai.column_map.items():
        if field_id == "documentation":
            doc_col = letter
            break
    if doc_col and doc_col not in fai.scored_section_columns:
        # Find insert position: count score columns before doc_col
        score_cols_sorted = sorted(fai.scored_section_columns.keys())
        insert_pos = sum(1 for c in score_cols_sorted if c < doc_col)
        row.insert(3 + insert_pos, 1)  # 3 = offset for A/B/C metadata

    row.append(scorecard.key_strengths or "")                        # N: Key Strengths
    row.append(scorecard.opportunities or "")                        # O: Opportunities
    row.append(dialpad_link)                                         # P: Dialpad Link

    # Append AI reasoning columns (source + confidence/reasoning pairs):
    row.append("ai")                                                 # Q: Source
    for col_letter in sorted(fai.scored_section_columns.keys()):
        section_id = fai.scored_section_columns[col_letter]
        section = sections_by_id.get(section_id, {})
        row.append(section.get("confidence", ""))                    # confidence
        row.append(section.get("reasoning", ""))                     # reasoning

    # Append caller metadata columns
    row.append(scorecard.call_summary or "")                         # AJ: Call Summary
    row.append(scorecard.caller_name or "")                          # AK: Caller Name
    row.append(scorecard.caller_phone or "")                         # AL: Caller Phone

    result = sheet.append_row(row, value_input_option="USER_ENTERED")
    # Extract the row number from the update response
    updated_range = result.get("updates", {}).get("updatedRange", "")
    try:
        row_num = int(updated_range.split("!")[1].split(":")[0][1:])
    except (IndexError, ValueError):
        row_num = -1

    return row_num


def update_scorecard_reasoning(
    row_num: int,
    sections: list[dict],
    config: TeamConfig,
    key_strengths: str = "",
    opportunities: str = "",
) -> None:
    """
    Update reasoning/confidence columns, feedback columns (N-O),
    in Form Responses AI. Score columns (D-M) are left untouched —
    the original AI draft scores are preserved as an audit trail.
    """
    fai = config.sheets.form_responses_ai
    sheet = _get_sheet()
    sections_by_id = {s["id"]: s for s in sections}

    # --- Feedback columns N-O (needed for Analyst_History enrichment) ---
    if key_strengths or opportunities:
        print(f"[sheets] update_reasoning: writing N-O at row {row_num}...")
        sheet.update(
            f"N{row_num}:O{row_num}",
            [[key_strengths, opportunities]],
            value_input_option="USER_ENTERED",
        )
        print("[sheets] update_reasoning: N-O done.")

    # --- Reasoning columns Q-AI (source + 9 x [confidence, reasoning]) ---
    reasoning_values = ["ai"]
    for col_letter in sorted(fai.scored_section_columns.keys()):
        section_id = fai.scored_section_columns[col_letter]
        section = sections_by_id.get(section_id, {})
        reasoning_values.append(section.get("confidence", ""))
        reasoning_values.append(section.get("reasoning", ""))

    print(f"[sheets] update_reasoning: writing Q-AI at row {row_num}...")
    sheet.update(
        f"Q{row_num}:AI{row_num}", [reasoning_values],
        value_input_option="USER_ENTERED",
    )
    print("[sheets] update_reasoning: Q-AI done.")


def write_approved_to_form_responses_1(
    sections: list[dict],
    config: TeamConfig,
    key_strengths: str = "",
    opportunities: str = "",
    timestamp: str = "",
    manager_email: str = "",
    agent_name: str = "",
    dialpad_link: str = "",
) -> int:
    """
    Write the manager-approved scorecard directly to Form Responses 1.
    Returns the 1-indexed row number in Form Responses 1.
    """
    fai = config.sheets.form_responses_ai
    print("[sheets] write_approved: opening Form Responses 1...")
    spreadsheet = _get_spreadsheet()
    form_1 = spreadsheet.worksheet(config.sheets.form_responses_1_tab)

    sections_by_id = {s["id"]: s for s in sections}
    score_col_ids = [
        fai.scored_section_columns[c]
        for c in sorted(fai.scored_section_columns.keys())
    ]

    # Build row A-P with approved scores
    row = [
        timestamp,                                                       # A
        manager_email,                                                   # B
        agent_name,                                                      # C
    ]
    for section_id in score_col_ids[:8]:  # D-K (8 scored sections before Documentation)
        row.append(_format_score(sections_by_id.get(section_id, {})))
    doc = sections_by_id.get("documentation", {})
    row.append(doc.get("score", 1) if doc else 1)                        # L
    row.append(_format_score(sections_by_id.get(score_col_ids[8], {})))  # M (CRI)
    row.append(key_strengths)                                            # N
    row.append(opportunities)                                            # O
    row.append(dialpad_link)                                             # P

    print("[sheets] write_approved: appending row...")
    result = form_1.append_row(row, value_input_option="USER_ENTERED")
    print("[sheets] write_approved: done.")
    updated_range = result.get("updates", {}).get("updatedRange", "")
    try:
        form_1_row = int(updated_range.split("!")[1].split(":")[0][1:])
    except (IndexError, ValueError):
        form_1_row = -1

    return form_1_row


def trigger_apps_script(form_1_row_num: int) -> dict:
    """POST to the Apps Script web app to trigger the email pipeline."""
    import httpx

    url = os.getenv("APPS_SCRIPT_WEBAPP_URL", "")
    if not url:
        raise RuntimeError(
            "APPS_SCRIPT_WEBAPP_URL not set — deploy Main.js as a web app "
            "and add the URL to .env"
        )

    response = httpx.post(
        url,
        json={"rowNumber": form_1_row_num},
        timeout=60.0,
        follow_redirects=True,
    )
    response.raise_for_status()
    return response.json()

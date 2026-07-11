"""Google Sheets writer — Stage-1 draft projection + Apps Script trigger.

Post-cutover surface (CutoverDesign §8 slice-5 cleanup): the engine owns
scoring, so this module keeps only what the engine pipeline still needs
from Sheets:

    Stage 1 — write_draft_to_fr_ai   (AI scoring → FR-AI draft row)
    Score_Audit append               (action-level audit tab)
    trigger_apps_script              (email dispatch by AH row number)

The pre-cutover four-stage flow (analyst edits → destination write →
ARRAYFORMULA readback → Analyst_History finalize, plus the Mails
runtime lookup) was deleted in slice 5; on approve, the DB transitions
and ``sheets_projection.project_evaluation`` writes the FR-AI overwrite
and the Analyst_History row from the qa.* record.

FR-AI and Analyst_History use the derived layout
(``config.history_layout``).

``trigger_apps_script`` posts the Analyst_History row number to the
team's Apps Script web app, which reads the populated row and
dispatches the QA evaluation email.

Setup:
  1. Create a Google Cloud service account
  2. Share each team's QA sheet with the service account email (Editor)
  3. Set GOOGLE_SERVICE_ACCOUNT_JSON=/path/to/credentials.json (or inline JSON)
  4. Set GOOGLE_SHEETS_ID (or GOOGLE_SHEETS_ID_<TEAM>) per team
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

import gspread
from google.oauth2.service_account import Credentials

from backend.config import history_layout
from backend.config.env import env_for_team
from backend.config.history_layout import col_index_to_letter, col_letter_to_index
from backend.models.scorecard import ScorecardWithMeta

if TYPE_CHECKING:
    from backend.config.team_config import TeamConfig
    from backend.models.formula import RubricSection

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

YN_DISPLAY = {
    "Y": "Yes",
    "N": "No",
    "NA": "Not Applicable",
}


# ---------------------------------------------------------------------------
# Connection + tab helpers
# ---------------------------------------------------------------------------

def _get_spreadsheet(team_id: str):
    """Return a fresh gspread Spreadsheet object for *team_id*."""
    creds_env = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    sheet_id = env_for_team("GOOGLE_SHEETS_ID", team_id, legacy_ok=True)

    if not creds_env or not sheet_id:
        raise RuntimeError(
            f"GOOGLE_SERVICE_ACCOUNT_JSON and GOOGLE_SHEETS_ID (or "
            f"GOOGLE_SHEETS_ID_{team_id.upper()}) must be set"
        )

    if creds_env.strip().startswith("{"):
        creds_info = json.loads(creds_env)
        creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
    else:
        creds = Credentials.from_service_account_file(creds_env, scopes=SCOPES)

    client = gspread.authorize(creds)
    client.set_timeout(120)
    return client.open_by_key(sheet_id)


def _get_sheet(config: TeamConfig, tab_name: str | None = None):
    """Return the named worksheet for *config*'s team. Defaults to FR-AI."""
    tab = (
        tab_name
        or env_for_team("GOOGLE_SHEETS_TAB", config.team_id, legacy_ok=True)
        or config.sheets.form_responses_ai.tab_name
    )
    return _get_spreadsheet(config.team_id).worksheet(tab)


def _get_fr_ai_sheet(config: TeamConfig):
    """Return the Form Responses AI worksheet (where AI drafts land)."""
    return _get_sheet(config, config.sheets.form_responses_ai.tab_name)


def _sheets_configured(team_id: str) -> bool:
    """True if the env vars needed to reach *team_id*'s sheet are present."""
    return bool(os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")) and bool(
        env_for_team("GOOGLE_SHEETS_ID", team_id, legacy_ok=True)
    )


# ---------------------------------------------------------------------------
# Format + lookup helpers
# ---------------------------------------------------------------------------

def _format_call_started(call_started_at_utc) -> str:
    """Render the call's `date_connected` for COL_TIMESTAMP (col C).

    Returns the canonical sheet timestamp string ("MM/DD/YYYY HH:MM:SS",
    UTC clock) when supplied, or "" when the call's date_connected
    wasn't surfaced by Dialpad. Sentinel-blank lets `load_and_clean`'s
    PR-2 fallback path keep working for rows the backfill will visit
    later. See references/CallTimeOnAnalystHistory.md.
    """
    if call_started_at_utc is None:
        return ""
    # call_started_at_utc is a UTC-aware datetime per
    # _epoch_ms_to_utc_datetime. strftime emits the wall-clock value
    # (UTC) without a TZ marker, matching the existing convention.
    return call_started_at_utc.strftime("%m/%d/%Y %H:%M:%S")


def _format_ai_score(sec_def: RubricSection, ai_section: dict) -> str:
    """Convert AI-output section dict to a score-cell string.

    Explicit N/A wins regardless of score_type: when ``yn_value == "NA"``,
    render "Not Applicable" even for numeric sections (only valid when the
    team config declares ``na_applicable: true`` — the ScorecardSection
    validator enforces this when context is supplied).
    """
    if ai_section.get("yn_value") == "NA":
        return YN_DISPLAY["NA"]
    if sec_def.score_type == "yn":
        yn = ai_section.get("yn_value") or "NA"
        return YN_DISPLAY.get(yn, "Not Applicable")
    score = ai_section.get("score")
    return str(score) if score is not None else "N/A"


def _find_row_by_dialpad_link(sheet, dialpad_link: str) -> Optional[int]:
    """Return 1-indexed row number whose dialpad_link cell matches, else None.

    Used by Stage 1 for idempotency (re-scoring overwrites instead of
    appending duplicates).
    """
    if not dialpad_link:
        return None
    col_idx = history_layout.COL_DIALPAD_LINK + 1  # gspread cols are 1-indexed
    col_values = sheet.col_values(col_idx)
    for i, val in enumerate(col_values, start=1):
        if val == dialpad_link:
            return i
    return None


def _parse_appended_row_num(append_result: dict) -> int:
    """Extract the 1-indexed row number from a gspread append_row result.

    The cell ref returned by Sheets uses A1 notation; column letters can
    be 1+ chars (Sales' FR-AI extends past col Z). Match the trailing
    digits explicitly rather than assuming a single column letter.
    """
    updated_range = append_result.get("updates", {}).get("updatedRange", "")
    try:
        first_cell = updated_range.split("!")[1].split(":")[0]
        m = re.search(r"(\d+)$", first_cell)
        return int(m.group(1)) if m else -1
    except (IndexError, ValueError):
        return -1


def _next_data_row(sheet) -> int:
    """Return the 1-indexed row that should receive the next append.

    Inspects col A, col E (dialpad_link), and col G (first section) to
    survive irregular header rows where some prefix cols may be blank.
    Wider check than gspread's auto-detect-table path, which can latch
    onto a partial column and produce an offset write.
    """
    a_len = len(sheet.col_values(1))
    e_len = len(sheet.col_values(history_layout.COL_DIALPAD_LINK + 1))
    g_len = len(sheet.col_values(history_layout.COL_OVERALL_SCORE + 2))  # col G = first section
    return max(a_len, e_len, g_len) + 1


# ---------------------------------------------------------------------------
# Stage 1 — AI scoring → FR-AI
# ---------------------------------------------------------------------------

def write_draft_to_fr_ai(scorecard: ScorecardWithMeta, config: TeamConfig) -> int:
    """Stage 1: write AI scorecard draft to Form Responses AI.

    Layout uses ``config.history_layout``:
      * Cols A, C, E filled (agent_name, timestamp, dialpad_link)
      * Cols B, D, F (agent_email, evaluator_email, overall_score) blank —
        the post-approve projection fills them from the qa.* record
      * Section scores in canonical section_number order:
          - auto_value sections → sec.auto_value (e.g. Sales Q18 = "Yes")
          - manual sections → blank (analyst fills via dashboard)
          - AI-scored sections → formatted score / yn_value
      * Reasoning + confidence per section (blank for manual / auto_value)
      * Trailing: key_strengths, opportunities, call_summary, caller_name,
        caller_phone, source="ai"

    Idempotent on dialpad_link (col E): if a row with the same link
    exists, overwrites it; otherwise appends.

    Returns the FR-AI row number, or -1 if Sheets is not configured.
    """
    if not _sheets_configured(config.team_id):
        print(f"[stage_1] Sheets not configured for team '{config.team_id}' — skipping write.")
        return -1

    L = config.history_layout
    sheet = _get_fr_ai_sheet(config)

    sections_by_id = {s.id: s.model_dump() for s in scorecard.sections}

    row = [""] * L.total_width
    row[history_layout.COL_AGENT_NAME] = scorecard.agent_name or ""
    # Call-time initiative (PR-1): col C now holds the call's
    # `date_connected` from Dialpad (when the call actually happened),
    # not the draft/approval clock. Blank when get_call_details didn't
    # surface a date_connected — backfill (PR-2) will fill it later.
    # See references/CallTimeOnAnalystHistory.md.
    row[history_layout.COL_TIMESTAMP] = _format_call_started(scorecard.call_started_at_utc)
    row[history_layout.COL_DIALPAD_LINK] = scorecard.dialpad_link or ""
    # eval_approved_at (trailing column) is filled by the post-approve
    # projection. Leave blank here.

    for i, sec_def in enumerate(config.sections_by_number):
        if sec_def.auto_value is not None:
            row[L.col_score(i)] = sec_def.auto_value
            # reasoning + confidence stay blank
        elif sec_def.score_type in ("manual", "manual_yn"):
            # blank — analyst will fill via dashboard
            pass
        else:
            ai_section = sections_by_id.get(sec_def.id)
            if ai_section is not None:
                row[L.col_score(i)] = _format_ai_score(sec_def, ai_section)
                row[L.col_reasoning(i)] = ai_section.get("reasoning", "")
                row[L.col_confidence(i)] = ai_section.get("confidence", "")

    row[L.col_key_strengths] = scorecard.key_strengths or ""
    row[L.col_opportunities] = scorecard.opportunities or ""
    row[L.col_call_summary] = scorecard.call_summary or ""
    row[L.col_caller_name] = scorecard.caller_name or ""
    row[L.col_caller_phone] = scorecard.caller_phone or ""
    row[L.col_source] = "ai"

    end_letter = col_index_to_letter(L.total_width - 1)
    existing_row = _find_row_by_dialpad_link(sheet, scorecard.dialpad_link or "")
    target_row = existing_row if existing_row is not None else _next_data_row(sheet)

    # Colocated FR-AI: col F holds an ARRAYFORMULA whose output range
    # extends row-by-row. Writing any literal (including '') to col F
    # would clobber the formula's output for this row, so split the
    # write into A:E and G:end_letter and leave F untouched.
    colocated = config.sheets.score_destination.tab_name == config.sheets.form_responses_ai.tab_name
    if colocated:
        prefix = row[: history_layout.COL_OVERALL_SCORE]                # cols A-E
        suffix = row[history_layout.COL_OVERALL_SCORE + 1 :]            # cols G-end
        sheet.batch_update([
            {
                "range": f"A{target_row}:E{target_row}",
                "values": [prefix],
            },
            {
                "range": f"G{target_row}:{end_letter}{target_row}",
                "values": [suffix],
            },
        ], value_input_option="USER_ENTERED")
    else:
        sheet.update(
            f"A{target_row}:{end_letter}{target_row}",
            [row],
            value_input_option="USER_ENTERED",
        )

    if existing_row is not None:
        print(f"[stage_1] Overwrote FR-AI row {target_row} for dialpad_link match.")
    else:
        print(f"[stage_1] Wrote new FR-AI row {target_row}.")
    return target_row


# ---------------------------------------------------------------------------
# Score_Audit append (LookupToScore.md design)
# ---------------------------------------------------------------------------

def _build_score_audit_row(
    *,
    timestamp: str,
    api_key_role: str,
    evaluator_email: str,
    agent_email: str,
    agent_name: str,
    call_id: str,
    target_team: str,
    action: str,
    result_row: int | None,
    notes: str,
) -> list[str]:
    """Compose one Score_Audit row in COLUMNS order.

    Kept pure (no gspread, no clock) so tests can drive it directly and
    the live-Sheets append helper stays a thin wrapper.
    """
    from backend.config import score_audit as audit_cfg

    if action not in audit_cfg.ACTIONS:
        raise ValueError(f"unknown audit action '{action}'")
    if api_key_role not in audit_cfg.ROLES:
        raise ValueError(f"unknown api_key_role '{api_key_role}'")

    return [
        timestamp,
        api_key_role,
        evaluator_email,
        agent_email,
        agent_name,
        call_id,
        target_team,
        action,
        "" if result_row is None else str(result_row),
        notes,
    ]


def append_score_audit_row(
    *,
    api_key_role: str,
    evaluator_email: str,
    agent_email: str,
    agent_name: str,
    call_id: str,
    target_team: str,
    action: str,
    result_row: int | None = None,
    notes: str = "",
) -> int:
    """Append one row to the Score_Audit tab on the host spreadsheet.

    Writes timestamp as ISO 8601 UTC. Always targets the audit host
    team's spreadsheet (member_support) regardless of ``target_team`` —
    a privileged evaluator scoring a Sales call still appends to the
    same audit log.

    Returns the appended row number (1-indexed), or -1 if the gspread
    response can't be parsed (matches the rest of this module).
    """
    from backend.config import score_audit as audit_cfg

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    row = _build_score_audit_row(
        timestamp=timestamp,
        api_key_role=api_key_role,
        evaluator_email=evaluator_email,
        agent_email=agent_email,
        agent_name=agent_name,
        call_id=call_id,
        target_team=target_team,
        action=action,
        result_row=result_row,
        notes=notes,
    )

    sheet = _get_spreadsheet(audit_cfg.HOST_TEAM_ID).worksheet(audit_cfg.TAB_NAME)
    result = sheet.append_row(row, value_input_option="USER_ENTERED")
    return _parse_appended_row_num(result)


# ---------------------------------------------------------------------------
# Apps Script trigger
# ---------------------------------------------------------------------------

def trigger_apps_script(history_row_num: int, team_id: str) -> dict:
    """POST to the team's Apps Script web app to dispatch the QA email.

    The Apps Script reads ``Analyst_History`` row ``history_row_num``
    (already written by the post-approve projection) and sends the
    evaluation email.
    """
    import httpx

    url = env_for_team("APPS_SCRIPT_WEBAPP_URL", team_id, legacy_ok=True)
    if not url:
        raise RuntimeError(
            f"APPS_SCRIPT_WEBAPP_URL not set for team '{team_id}' — deploy "
            f"Main.js as a web app and set APPS_SCRIPT_WEBAPP_URL"
            f"{'' if team_id == 'member_support' else '_' + team_id.upper()}"
        )

    response = httpx.post(
        url,
        json={"historyRowNumber": history_row_num},
        timeout=60.0,
        follow_redirects=True,
    )
    response.raise_for_status()
    return response.json()

"""Google Sheets writer — Score_Audit + Apps Script trigger + helpers.

Post-cutover surface (CutoverDesign §8 slice-5 cleanup, FR-AI retirement
2026-07-20): the engine owns scoring and drafts live solely in
qa.evaluations, so the ONLY Sheets writes anywhere in the pipeline are:

    Analyst_History — finalize-time projection
                      (``sheets_projection.project_evaluation``, which
                      borrows this module's worksheet/row helpers)
    Score_Audit     — action-level audit tab (``append_score_audit_row``)

The pre-cutover four-stage flow (analyst edits → destination write →
ARRAYFORMULA readback → Analyst_History finalize, plus the Mails runtime
lookup) was deleted in slice 5. The FR-AI draft write — Sheets-as-DB
Stage 1 — was retired 2026-07-20 after its tab hit the 965-row grid
limit and blocked the pipeline.

Analyst_History uses the derived layout (``config.history_layout``).

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
    """Return the named worksheet for *config*'s team. Defaults to
    Analyst_History (the FR-AI draft tab retired 2026-07-20)."""
    tab = (
        tab_name
        or env_for_team("GOOGLE_SHEETS_TAB", config.team_id, legacy_ok=True)
        or config.sheets.analyst_history.tab_name
    )
    return _get_spreadsheet(config.team_id).worksheet(tab)


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
# NOTE: `write_draft_to_fr_ai` (Sheets-as-DB Stage 1: AI drafts landing
# on the Form Responses AI tab ahead of analyst approval) was RETIRED
# 2026-07-20. The tab hit its 965-row grid limit and — because the write
# ran ahead of the Postgres dual-write — a deprecated projection blocked
# the whole scoring pipeline. Drafts live solely in qa.evaluations;
# Sheets writes are Analyst_History (finalize projection) + Score_Audit.

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

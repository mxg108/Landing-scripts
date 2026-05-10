"""Google Sheets writer — Phase 2, four-stage pipeline.

Replaces the monolithic ``append_scorecard_row`` /
``update_scorecard_reasoning`` / ``write_approved_to_form_responses_1``
trio with four staged functions:

    Stage 1 — write_draft_to_fr_ai          (AI scoring → FR-AI)
    Stage 1.5 (in route) — apply analyst edits to FR-AI
    Stage 2 — write_to_score_destination    (on Approve → FR1 / Scores tab)
    Stage 3 — read_score_and_writeback      (poll readback col → FR-AI col F)
    Stage 4 — finalize_to_analyst_history   (FR-AI → Analyst_History)

Both FR-AI and Analyst_History use the derived layout
(``config.history_layout``); the only hardcoded letters live in the
per-team ``score_destination`` block (mirrors legacy form layouts that
ARRAYFORMULAs depend on).

``trigger_apps_script`` continues to send the destination-tab row to
Apps Script during Phase B (a) — Apps Script will start reading from
Analyst_History instead in Phase C.

Setup:
  1. Create a Google Cloud service account
  2. Share each team's QA sheet with the service account email (Editor)
  3. Set GOOGLE_SERVICE_ACCOUNT_JSON=/path/to/credentials.json (or inline JSON)
  4. Set GOOGLE_SHEETS_ID (or GOOGLE_SHEETS_ID_<TEAM>) per team
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

import gspread
from google.oauth2.service_account import Credentials

from backend.config import history_layout
from backend.config.env import env_for_team
from backend.config.history_layout import col_index_to_letter, col_letter_to_index
from backend.models.scorecard import ScorecardWithMeta

if TYPE_CHECKING:
    from backend.config.team_config import SectionDef, TeamConfig

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

def _format_ai_score(sec_def: SectionDef, ai_section: dict) -> str:
    """Convert AI-output section dict to a score-cell string."""
    if sec_def.score_type == "yn":
        yn = ai_section.get("yn_value") or "NA"
        return YN_DISPLAY.get(yn, "Not Applicable")
    score = ai_section.get("score")
    return str(score) if score is not None else "N/A"


def _combine_feedback(key_strengths: str, opportunities: str) -> str:
    """Combine key_strengths + opportunities into a single feedback cell.

    Used by teams whose score destination has one Feedback column rather
    than separate strengths/opportunities cells (Sales' Scores tab → X).
    """
    parts = []
    if key_strengths:
        parts.append(f"What went well:\n{key_strengths}")
    if opportunities:
        parts.append(f"Opportunities for improvement:\n{opportunities}")
    return "\n\n".join(parts)


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


def _lookup_agent_email(agent_name: str, config: TeamConfig) -> Optional[str]:
    """Resolve an agent's email from the Mails tab. Case-insensitive.

    Mails layout: A=Name, B=Email, (C=Supervisor, D=Canonical Name).
    """
    if not agent_name:
        return None
    try:
        mails_sheet = _get_sheet(config, config.sheets.mails_tab)
        rows = mails_sheet.get_all_values()
    except Exception as e:
        print(f"[sheets] _lookup_agent_email failed: {e}")
        return None
    needle = agent_name.strip().lower()
    for row in rows[1:]:  # skip header
        if len(row) >= 2 and row[0].strip().lower() == needle:
            return row[1].strip()
    return None


def _parse_appended_row_num(append_result: dict) -> int:
    """Extract the 1-indexed row number from a gspread append_row result."""
    updated_range = append_result.get("updates", {}).get("updatedRange", "")
    try:
        return int(updated_range.split("!")[1].split(":")[0][1:])
    except (IndexError, ValueError):
        return -1


# ---------------------------------------------------------------------------
# Stage 1 — AI scoring → FR-AI
# ---------------------------------------------------------------------------

def write_draft_to_fr_ai(scorecard: ScorecardWithMeta, config: TeamConfig) -> int:
    """Stage 1: write AI scorecard draft to Form Responses AI.

    Layout uses ``config.history_layout``:
      * Cols A, C, E filled (agent_name, timestamp, dialpad_link)
      * Col B (agent_email) blank — filled at Stage 4 from Mails lookup
      * Col D (evaluator_email) blank — filled at Stage 2 from session
      * Col F (overall_score) blank — filled at Stage 3 from readback
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
    row[history_layout.COL_TIMESTAMP] = datetime.now(timezone.utc).strftime("%m/%d/%Y %H:%M:%S")
    row[history_layout.COL_DIALPAD_LINK] = scorecard.dialpad_link or ""

    for i, sec_def in enumerate(config.sections_by_number):
        if sec_def.auto_value is not None:
            row[L.col_score(i)] = sec_def.auto_value
            # reasoning + confidence stay blank
        elif sec_def.score_type == "manual":
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

    existing_row = _find_row_by_dialpad_link(sheet, scorecard.dialpad_link or "")
    if existing_row is not None:
        end_letter = col_index_to_letter(L.total_width - 1)
        sheet.update(
            f"A{existing_row}:{end_letter}{existing_row}",
            [row],
            value_input_option="USER_ENTERED",
        )
        print(f"[stage_1] Overwrote FR-AI row {existing_row} for dialpad_link match.")
        return existing_row

    result = sheet.append_row(row, value_input_option="USER_ENTERED")
    return _parse_appended_row_num(result)


# ---------------------------------------------------------------------------
# Stage 1.5 — Analyst edits → FR-AI
# ---------------------------------------------------------------------------

def apply_analyst_edits_to_fr_ai(
    fr_ai_row_num: int,
    sections: list[dict],
    config: TeamConfig,
    key_strengths: str = "",
    opportunities: str = "",
) -> None:
    """Stage 1.5: apply analyst's dashboard edits to the FR-AI row.

    Writes (or overwrites) section scores, reasoning, confidence, and
    trailing feedback cells. Manual sections receive whatever the
    analyst entered. Auto_value sections are skipped (writer-managed).
    """
    L = config.history_layout
    sheet = _get_fr_ai_sheet(config)
    sections_by_id = {s["id"]: s for s in sections}

    # We update each row's score + reasoning + confidence per section,
    # plus the trailing feedback cells, in a single batch_update.
    updates: list[dict] = []

    for i, sec_def in enumerate(config.sections_by_number):
        if sec_def.auto_value is not None:
            continue  # writer-managed; analyst can't override

        section = sections_by_id.get(sec_def.id)
        if section is None:
            continue

        score_value = (
            _format_ai_score(sec_def, section)
            if sec_def.score_type != "manual"
            else str(section.get("score", ""))
        )
        score_letter = col_index_to_letter(L.col_score(i))
        reasoning_letter = col_index_to_letter(L.col_reasoning(i))
        confidence_letter = col_index_to_letter(L.col_confidence(i))

        updates.append({
            "range": f"{score_letter}{fr_ai_row_num}",
            "values": [[score_value]],
        })
        updates.append({
            "range": f"{reasoning_letter}{fr_ai_row_num}",
            "values": [[section.get("reasoning", "")]],
        })
        updates.append({
            "range": f"{confidence_letter}{fr_ai_row_num}",
            "values": [[section.get("confidence", "")]],
        })

    ks_letter = col_index_to_letter(L.col_key_strengths)
    op_letter = col_index_to_letter(L.col_opportunities)
    updates.append({
        "range": f"{ks_letter}{fr_ai_row_num}:{op_letter}{fr_ai_row_num}",
        "values": [[key_strengths, opportunities]],
    })

    print(f"[stage_1.5] Applying {len(updates)} edits to FR-AI row {fr_ai_row_num}...")
    sheet.batch_update(updates, value_input_option="USER_ENTERED")
    print(f"[stage_1.5] Done.")


# ---------------------------------------------------------------------------
# Stage 2 — On Approve → score destination
# ---------------------------------------------------------------------------

def write_to_score_destination(
    fr_ai_row_num: int,
    config: TeamConfig,
    evaluator_email: str,
) -> int:
    """Stage 2: read FR-AI row, write section scores + metadata to the
    per-team score destination tab (MS: Form Responses 1; Sales: Scores).

    Also writes ``evaluator_email`` to FR-AI col D so Stage 4 can carry
    it forward into Analyst_History.

    Section column placement comes from
    ``config.sheets.score_destination.section_score_columns`` — the only
    hardcoded-letters mapping in the system. Auto-value sections write
    their declared value; manual sections fall back to
    ``manual_default_value`` if the analyst left the FR-AI cell blank.

    Returns the destination tab's appended row number.
    """
    L = config.history_layout
    sd = config.sheets.score_destination

    fr_ai_sheet = _get_fr_ai_sheet(config)
    fr_ai_row = fr_ai_sheet.row_values(fr_ai_row_num)
    fr_ai_row = (fr_ai_row + [""] * L.total_width)[: L.total_width]

    # Persist evaluator_email to FR-AI col D so Stage 4 carries it forward.
    eval_letter = col_index_to_letter(history_layout.COL_EVALUATOR_EMAIL)
    fr_ai_sheet.update(
        f"{eval_letter}{fr_ai_row_num}",
        [[evaluator_email]],
        value_input_option="USER_ENTERED",
    )

    # Compute destination row width
    section_letters = list(sd.section_score_columns.keys())
    metadata_letters = list(sd.metadata_cols.values())
    all_letters = section_letters + metadata_letters
    dest_width = max(col_letter_to_index(L_) for L_ in all_letters) + 1
    dest_row = [""] * dest_width

    # Section scores
    sections_ordered = config.sections_by_number
    section_idx_by_id = {s.id: i for i, s in enumerate(sections_ordered)}
    for letter, section_id in sd.section_score_columns.items():
        col_idx = col_letter_to_index(letter)
        sec_def = config.scoring_id_to_section.get(section_id)
        if not sec_def:
            continue
        layout_idx = section_idx_by_id.get(section_id)
        if layout_idx is None:
            continue
        fr_ai_value = fr_ai_row[L.col_score(layout_idx)]

        if sec_def.auto_value is not None:
            dest_row[col_idx] = sec_def.auto_value
        elif sec_def.score_type == "manual" and not fr_ai_value:
            dest_row[col_idx] = sec_def.manual_default_value or ""
        else:
            dest_row[col_idx] = fr_ai_value

    # Metadata
    key_strengths = fr_ai_row[L.col_key_strengths]
    opportunities = fr_ai_row[L.col_opportunities]
    metadata_values = {
        "timestamp": fr_ai_row[history_layout.COL_TIMESTAMP],
        "agent_name": fr_ai_row[history_layout.COL_AGENT_NAME],
        "dialpad_link": fr_ai_row[history_layout.COL_DIALPAD_LINK],
        "manager_email": evaluator_email,        # MS schema name
        "evaluator_email": evaluator_email,      # Sales schema name
        "key_strengths": key_strengths,
        "opportunities": opportunities,
        "feedback_combined": _combine_feedback(key_strengths, opportunities),
    }
    for field, letter in sd.metadata_cols.items():
        col_idx = col_letter_to_index(letter)
        if field in metadata_values:
            dest_row[col_idx] = metadata_values[field]
        else:
            print(f"[stage_2] WARNING: metadata field '{field}' not produced by writer; col {letter} stays blank.")

    dest_sheet = _get_sheet(config, sd.tab_name)
    print(f"[stage_2] Appending row to '{sd.tab_name}'...")
    result = dest_sheet.append_row(dest_row, value_input_option="USER_ENTERED")
    dest_row_num = _parse_appended_row_num(result)
    print(f"[stage_2] Done. Row {dest_row_num}.")
    return dest_row_num


# ---------------------------------------------------------------------------
# Stage 3 — Poll readback → FR-AI col F
# ---------------------------------------------------------------------------

async def read_score_and_writeback(
    dest_row_num: int,
    fr_ai_row_num: int,
    config: TeamConfig,
) -> str:
    """Stage 3: poll the score destination's readback col with bounded
    retries until the ARRAYFORMULA-computed overall score appears, then
    write that value back to FR-AI col F.

    Polling: 5 attempts × 800 ms = 4 s ceiling (matches PhaseOne's
    documented 3-4 s ARRAYFORMULA buffer).

    Returns the score string written, or '' if it never appeared.
    """
    sd = config.sheets.score_destination
    dest_sheet = _get_sheet(config, sd.tab_name)

    overall = ""
    max_attempts = 5
    delay_s = sd.arrayformula_buffer_seconds / max_attempts
    for attempt in range(max_attempts):
        cell_value = dest_sheet.acell(f"{sd.score_readback_col}{dest_row_num}").value
        if cell_value:
            overall = cell_value
            print(f"[stage_3] Readback hit on attempt {attempt + 1}: '{overall}'")
            break
        if attempt < max_attempts - 1:
            await asyncio.sleep(delay_s)

    if not overall:
        print(
            f"[stage_3] WARNING: readback {sd.score_readback_col}{dest_row_num} "
            f"stayed blank after {sd.arrayformula_buffer_seconds}s — formula may "
            f"have failed."
        )
        return ""

    fr_ai_sheet = _get_fr_ai_sheet(config)
    score_letter = col_index_to_letter(history_layout.COL_OVERALL_SCORE)
    fr_ai_sheet.update(
        f"{score_letter}{fr_ai_row_num}",
        [[overall]],
        value_input_option="USER_ENTERED",
    )
    return overall


# ---------------------------------------------------------------------------
# Stage 4 — FR-AI → Analyst_History
# ---------------------------------------------------------------------------

def finalize_to_analyst_history(
    fr_ai_row_num: int,
    evaluator_email: str,
    config: TeamConfig,
) -> int:
    """Stage 4: copy the now-complete FR-AI row to Analyst_History.

    Resolves agent_email via the Mails lookup, sets evaluator_email,
    refreshes the timestamp to "approval time" (UTC). Both tabs share
    the derived layout — this is essentially a row-copy with a few
    metadata cells overridden.

    Returns the Analyst_History row number.
    """
    L = config.history_layout

    fr_ai_sheet = _get_fr_ai_sheet(config)
    fr_ai_row = fr_ai_sheet.row_values(fr_ai_row_num)
    fr_ai_row = (fr_ai_row + [""] * L.total_width)[: L.total_width]

    agent_name = fr_ai_row[history_layout.COL_AGENT_NAME]
    agent_email = _lookup_agent_email(agent_name, config) or ""

    history_row = list(fr_ai_row)
    history_row[history_layout.COL_AGENT_EMAIL] = agent_email
    history_row[history_layout.COL_EVALUATOR_EMAIL] = evaluator_email
    history_row[history_layout.COL_TIMESTAMP] = datetime.now(timezone.utc).strftime(
        "%m/%d/%Y %H:%M:%S"
    )

    history_sheet = _get_sheet(config, config.sheets.analyst_history.tab_name)
    print(f"[stage_4] Appending to Analyst_History (agent_email='{agent_email}')...")
    result = history_sheet.append_row(history_row, value_input_option="USER_ENTERED")
    history_row_num = _parse_appended_row_num(result)
    print(f"[stage_4] Done. Row {history_row_num}.")
    return history_row_num


# ---------------------------------------------------------------------------
# Apps Script trigger (Phase B (a): still passes destination row;
# Phase C will switch to Analyst_History row)
# ---------------------------------------------------------------------------

def trigger_apps_script(dest_row_num: int, team_id: str) -> dict:
    """POST to the Apps Script web app to trigger the email pipeline.

    Phase B (a) note: still passes the score-destination tab's row
    number (``dest_row_num``) because Apps Script currently reads from
    Form Responses 1. Phase C updates Apps Script to read from
    Analyst_History; at that point this should switch to the history
    row number.
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
        json={"rowNumber": dest_row_num},
        timeout=60.0,
        follow_redirects=True,
    )
    response.raise_for_status()
    return response.json()

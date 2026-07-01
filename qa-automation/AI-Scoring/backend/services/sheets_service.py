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

import asyncio
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
    # Call-time initiative (PR-1): col C now holds the call's
    # `date_connected` from Dialpad (when the call actually happened),
    # not the draft/approval clock. Blank when get_call_details didn't
    # surface a date_connected — backfill (PR-2) will fill it later.
    # See references/CallTimeOnAnalystHistory.md.
    row[history_layout.COL_TIMESTAMP] = _format_call_started(scorecard.call_started_at_utc)
    row[history_layout.COL_DIALPAD_LINK] = scorecard.dialpad_link or ""
    # eval_approved_at (new trailing column) is filled at Stage 4 by
    # finalize_to_analyst_history. Leave blank here.

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

        # Manual sections carry an analyst-entered value rather than the
        # AI's. `manual` stores a 1-5 score; `manual_yn` stores a Y/N/NA
        # in yn_value (rendered via the same display map as AI yn).
        # Explicit N/A (yn_value="NA") wins for both shapes — analyst can
        # mark a manual numeric section N/A when the team config allows.
        if section.get("yn_value") == "NA":
            score_value = YN_DISPLAY["NA"]
        elif sec_def.score_type == "manual":
            score = section.get("score")
            score_value = str(score) if score is not None else ""
        elif sec_def.score_type == "manual_yn":
            yn = section.get("yn_value") or "NA"
            score_value = YN_DISPLAY.get(yn, "Not Applicable")
        else:
            score_value = _format_ai_score(sec_def, section)
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

    Call-time initiative (PR-1) — see
    references/CallTimeOnAnalystHistory.md: the ``timestamp`` cell in
    ``metadata_cols`` (FR1 col A for MS, col C for Sales) is sourced
    from ``fr_ai_row[COL_TIMESTAMP]``, which Stage 1 now populates from
    the call's ``date_connected`` instead of the draft clock. The Apps
    Script email's "Evaluation Date" line therefore renders as the
    call date going forward — usually what agents/managers actually
    want to see. Renaming that label on the GAS side is a follow-up.

    Returns the destination tab's appended row number.
    """
    L = config.history_layout
    sd = config.sheets.score_destination

    fr_ai_sheet = _get_fr_ai_sheet(config)

    # Persist evaluator_email to FR-AI col D so Stage 4 carries it forward.
    eval_letter = col_index_to_letter(history_layout.COL_EVALUATOR_EMAIL)
    fr_ai_sheet.update(
        f"{eval_letter}{fr_ai_row_num}",
        [[evaluator_email]],
        value_input_option="USER_ENTERED",
    )

    # Colocated destination: when score_destination IS the FR-AI tab
    # (Sales post-collapse), the FR-AI row already holds every value the
    # destination would receive — sections, metadata, and the
    # ARRAYFORMULA at score_readback_col fires on the same row. Skip the
    # append and return fr_ai_row_num so Stage 3 polls the FR-AI row.
    if sd.tab_name == config.sheets.form_responses_ai.tab_name:
        print(f"[stage_2] Destination colocated with FR-AI ('{sd.tab_name}'); skipping append.")
        return fr_ai_row_num

    fr_ai_row = fr_ai_sheet.row_values(fr_ai_row_num)
    fr_ai_row = (fr_ai_row + [""] * L.total_width)[: L.total_width]

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
        elif sec_def.score_type in ("manual", "manual_yn") and not fr_ai_value:
            dest_row[col_idx] = sec_def.manual_default_value or ""
        else:
            dest_row[col_idx] = fr_ai_value

    # Metadata
    key_strengths = fr_ai_row[L.col_key_strengths]
    opportunities = fr_ai_row[L.col_opportunities]
    metadata_values = {
        # "timestamp" semantically = the call's date_connected since the
        # call-time initiative (PR-1) flipped col C. Pre-cutover rows
        # passing through Stage 2 (rare; only re-approvals of historical
        # drafts) still carry the legacy eval-time value in col C —
        # acceptable transient. Backfill (PR-2) makes the semantic
        # uniform across all rows.
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

    # Colocated destination: readback cell IS FR-AI col F. The
    # ARRAYFORMULA already wrote the value there — writing it back as a
    # literal would clobber the formula's output for this row. Skip the
    # writeback in that case.
    if sd.tab_name == config.sheets.form_responses_ai.tab_name:
        print(f"[stage_3] Destination colocated with FR-AI; readback already at FR-AI col F. Skipping writeback.")
        return overall

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
    and stamps the approval time on the new ``col_eval_approved_at``
    trailing column.

    Call-time initiative (PR-1) — see
    references/CallTimeOnAnalystHistory.md: this no longer overrides
    col C. COL_TIMESTAMP was set at Stage 1 (`write_draft_to_fr_ai`)
    from the call's `date_connected`; it travels untouched into
    Analyst_History. The approval-time UTC string that used to live
    in col C now lives in `col_eval_approved_at`.

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
    # NOTE: COL_TIMESTAMP intentionally NOT touched here — it holds
    # call_connected from Stage 1. The approval clock writes to the new
    # trailing column instead.
    history_row[L.col_eval_approved_at] = datetime.now(timezone.utc).strftime(
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
    (already finalized by Stage 4) and sends the evaluation email.
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

"""DB → Sheets projection — cutover slice 2 (CutoverDesign.md §3a).

One job, one rule: **render sheet rows from a qa.evaluations row; never read
cell values to construct DB state.** This is the post-flip replacement for
the Stage-2/3/4 sheet flow — finalized evaluations land in Analyst_History
straight from Postgres, the ARRAYFORMULA never runs, and the only Sheets
reads are row-position lookups for idempotent overwrites.

Analyst_History is the ONLY projection target (the FR-AI draft tab —
Sheets-as-DB Stage 1 — was retired 2026-07-20 after its grid limit took
the pipeline down; drafts live solely in qa.evaluations now). The
projection writes every field the DB knows (agent_email,
evaluator_email, overall_score, eval_approved_at) — downstream consumers
(GAS email pipeline, dashboards via history_service) read the same
columns as before.

Failure semantics: the DB row is truth, so projection failures are
retryable — callers log and continue; the nightly reproject sweep
(CutoverDesign §3a, follow-up) re-renders anything that missed.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Optional

from backend.config import history_layout
from backend.config.history_layout import col_index_to_letter
from backend.services.sheets_service import (
    YN_DISPLAY,
    _find_row_by_dialpad_link,
    _get_sheet,
    _next_data_row,
    _sheets_configured,
)

if TYPE_CHECKING:
    from backend.config.team_config import TeamConfig

logger = logging.getLogger(__name__)

_SHEET_TS_FORMAT = "%m/%d/%Y %H:%M:%S"


# ---------------------------------------------------------------------------
# Row rendering (pure)
# ---------------------------------------------------------------------------

def _render_section_value(section: dict[str, Any]) -> str:
    """DB section row -> score-cell string, mirroring the legacy writer:
    numeric scores as digits, binary/NA via YN_DISPLAY."""
    if section["binary_value"] is not None:
        return YN_DISPLAY.get(section["binary_value"], "Not Applicable")
    if section["numeric_score"] is not None:
        return str(section["numeric_score"])
    return ""


def _render_ts(value: Any) -> str:
    return value.strftime(_SHEET_TS_FORMAT) if value else ""


def _metadata_dict(evaluation: Any) -> dict:
    """dialpad_call_metadata JSONB → dict (asyncpg returns a JSON string
    without a custom codec; tolerate str, dict, or NULL). ``.get`` works
    on both asyncpg Records and plain-dict test fixtures."""
    raw = evaluation.get("dialpad_call_metadata")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    return raw if isinstance(raw, dict) else {}


def _render_disposition(evaluation: Any) -> str:
    """"Category — Sub" display cell; blank when the CC match found none."""
    category = evaluation.get("dialpad_disposition_category") or ""
    sub = evaluation.get("dialpad_disposition") or ""
    if category and sub:
        return f"{category} — {sub}"
    return category or sub


def _render_sop_references(metadata: dict) -> str:
    """Newline-joined "SOP n: Title" footnotes from the pulpo_docs
    provenance — numbering matches the [SOP n] citations the scoring
    prompt used. Falls back to the bare sop_used title for rows written
    before provenance existed."""
    docs = metadata.get("pulpo_docs")
    if isinstance(docs, list) and docs:
        return "\n".join(
            f"SOP {i}: {doc.get('title', '')}"
            for i, doc in enumerate(docs, start=1)
            if isinstance(doc, dict) and doc.get("title")
        )
    return metadata.get("sop_used") or ""


def _render_overall(value: Any) -> str:
    """Engine scores are NUMERIC(5,1); render one decimal, trimming a
    trailing .0 so clean integers keep the legacy look (87, not 87.0)."""
    if value is None:
        return ""
    text = f"{float(value):.1f}"
    return text[:-2] if text.endswith(".0") else text


def build_projection_row(
    evaluation: Any, sections: list[dict[str, Any]], config: "TeamConfig"
) -> list[str]:
    """qa.evaluations row + qa.evaluation_sections rows -> sheet row (full
    HistoryLayout width). Serves FR-AI and Analyst_History identically."""
    L = config.history_layout
    by_id = {s["section_id"]: s for s in sections}

    row = [""] * L.total_width
    row[history_layout.COL_AGENT_NAME] = evaluation["agent_name_raw"] or ""
    row[history_layout.COL_AGENT_EMAIL] = evaluation["agent_email"] or ""
    row[history_layout.COL_TIMESTAMP] = _render_ts(evaluation["call_connected_at"])
    row[history_layout.COL_EVALUATOR_EMAIL] = evaluation["evaluator_email"] or ""
    row[history_layout.COL_DIALPAD_LINK] = evaluation["dialpad_link"] or ""
    row[history_layout.COL_OVERALL_SCORE] = _render_overall(evaluation["overall_score"])

    for i, sec_def in enumerate(config.sections_by_number):
        section = by_id.get(sec_def.id)
        if section is None:
            continue  # unfilled manual slot — cell stays blank, like today
        row[L.col_score(i)] = _render_section_value(section)
        row[L.col_reasoning(i)] = section.get("reasoning") or ""
        row[L.col_confidence(i)] = (section.get("confidence") or "").lower()

    row[L.col_key_strengths] = evaluation["key_strengths"] or ""
    row[L.col_opportunities] = evaluation["opportunities"] or ""
    row[L.col_call_summary] = evaluation["call_summary"] or ""
    row[L.col_caller_name] = evaluation["caller_name"] or ""
    row[L.col_caller_phone] = evaluation["caller_phone"] or ""
    row[L.col_source] = evaluation["source"] or ""
    row[L.col_eval_approved_at] = _render_ts(evaluation["approved_at"])
    # Call-metadata trailing columns (DispositionDesign §5 / PulpoConnection
    # §4.2) — the GAS email reads these to render the disposition line and
    # the SOP-reference footnotes.
    metadata = _metadata_dict(evaluation)
    ai_csat = evaluation.get("ai_csat")
    row[L.col_disposition] = _render_disposition(evaluation)
    row[L.col_ai_csat] = str(ai_csat) if ai_csat is not None else ""
    row[L.col_sop_references] = _render_sop_references(metadata)
    return row


# ---------------------------------------------------------------------------
# Sheet writes (idempotent on dialpad_link)
# ---------------------------------------------------------------------------

def _write_row(sheet, row: list[str], dialpad_link: str, config: "TeamConfig") -> int:
    """Overwrite the row matching dialpad_link, else append. Returns the
    1-indexed row number."""
    end_letter = col_index_to_letter(len(row) - 1)
    existing = _find_row_by_dialpad_link(sheet, dialpad_link)
    if existing is not None:
        sheet.update(f"A{existing}:{end_letter}{existing}", [row],
                     value_input_option="USER_ENTERED")
        return existing
    target = _next_data_row(sheet)
    sheet.update(f"A{target}:{end_letter}{target}", [row],
                 value_input_option="USER_ENTERED")
    return target


async def project_evaluation(
    conn: Any,
    evaluation_id: int,
    config: "TeamConfig",
    include_history: bool = False,
) -> Optional[int]:
    """Project one evaluation to Analyst_History (when `include_history`,
    i.e. the row is finalized — drafts are DB-only since the FR-AI tab
    retired). Returns the Analyst_History row number when written, for
    the Apps Script trigger.

    Raises on DB errors (the row is truth — §7.3 Phase C); logs and
    continues on Sheets errors (projections are retryable)."""
    evaluation = await conn.fetchrow(
        "SELECT * FROM qa.evaluations WHERE id = $1", evaluation_id
    )
    if evaluation is None:
        raise ValueError(f"no qa.evaluations row with id={evaluation_id}")
    sections = [
        dict(r) for r in await conn.fetch(
            "SELECT section_id, numeric_score, binary_value, confidence, reasoning "
            "FROM qa.evaluation_sections WHERE evaluation_id = $1",
            evaluation_id,
        )
    ]

    if not _sheets_configured(config.team_id):
        logger.warning("projection: Sheets not configured for %s — DB row %s stands alone",
                       config.team_id, evaluation_id)
        return None

    row = build_projection_row(evaluation, sections, config)
    link = evaluation["dialpad_link"] or ""
    history_row_num: Optional[int] = None

    if include_history:
        try:
            history_sheet = _get_sheet(config, config.sheets.analyst_history.tab_name)
            history_row_num = _write_row(history_sheet, row, link, config)
        except Exception:
            logger.exception("projection: Analyst_History write failed for eval %s — retryable",
                             evaluation_id)

    return history_row_num


async def tombstone_evaluation(dialpad_link: str, config: "TeamConfig") -> Optional[int]:
    """Blank the Analyst_History row of a DELETED evaluation
    (ScorecardActionsDesign §4.4.6).

    Sheets is a projection — a deleted DB row with a surviving sheet row
    is drift, not history. Callers must capture ``dialpad_link`` BEFORE
    deleting the qa.evaluations row; there is nothing left to resolve it
    from afterwards.

    Same failure semantics as project_evaluation: Sheets errors log and
    return None (the nightly reproject sweep can't repair a tombstone,
    but the audit row records the delete — a stale sheet row is visible,
    not silent). Returns the blanked 1-indexed row number, or None when
    the link is empty, Sheets is unconfigured, or no row matches."""
    if not dialpad_link:
        return None
    if not _sheets_configured(config.team_id):
        logger.warning("tombstone: Sheets not configured for %s — nothing to blank",
                       config.team_id)
        return None
    try:
        sheet = _get_sheet(config, config.sheets.analyst_history.tab_name)
        existing = _find_row_by_dialpad_link(sheet, dialpad_link)
        if existing is None:
            return None
        width = config.history_layout.total_width
        end_letter = col_index_to_letter(width - 1)
        sheet.update(f"A{existing}:{end_letter}{existing}", [[""] * width],
                     value_input_option="USER_ENTERED")
        return existing
    except Exception:
        logger.exception("tombstone: Analyst_History blank failed for link %s — "
                         "sheet row is stale until manually cleared", dialpad_link)
        return None


__all__ = ["project_evaluation", "build_projection_row", "tombstone_evaluation"]

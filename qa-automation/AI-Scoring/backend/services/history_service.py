"""Google Sheets data provider — reads Analyst_History tab."""

from __future__ import annotations

import os
from datetime import datetime, timedelta

import time as _time

import gspread
from google.oauth2.service_account import Credentials

from backend.models.dashboard import EvaluationRecord, SectionScore
from backend.services.data_provider import DataProvider

# ---------------------------------------------------------------------------
# Raw sheet data cache with 5-minute TTL
# ---------------------------------------------------------------------------
_sheet_cache: dict[str, tuple[float, list[list[str]]]] = {}
_CACHE_TTL = 300  # 5 minutes


def _get_cached_raw(key: str) -> list[list[str]] | None:
    """Return cached raw sheet data if fresh, else None."""
    entry = _sheet_cache.get(key)
    if entry is None:
        return None
    cached_at, data = entry
    if _time.time() - cached_at > _CACHE_TTL:
        del _sheet_cache[key]
        return None
    return data


def _set_cached_raw(key: str, data: list[list[str]]) -> None:
    _sheet_cache[key] = (_time.time(), data)

# ---------------------------------------------------------------------------
# Google Sheets authentication
# ---------------------------------------------------------------------------
_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

# ---------------------------------------------------------------------------
# Column index constants (0-based)
# ---------------------------------------------------------------------------
COL_AGENT_NAME = 0
COL_AGENT_EMAIL = 1
COL_TIMESTAMP = 2
COL_OVERALL_SCORE = 3

# Scored sections (indices 4-11)
SECTION_COLUMNS: dict[str, int] = {
    "greeting": 4,
    "purpose_of_call": 5,
    "matching_the_moment": 6,
    "process_adherence": 7,
    "call_resolution": 8,
    "communication": 9,
    "efficiency": 10,
    "documentation": 11,
}

# Y/N indicator sections (indices 12-13)
YN_COLUMNS: dict[str, int] = {
    "identity_validation": 12,
    "customer_resolution_indicator": 13,
}

COL_MANAGER_EMAIL = 14
COL_DIALPAD_LINK = 15
COL_KEY_STRENGTHS = 16
COL_IMPROVEMENTS = 17
COL_SOURCE = 18

# Extended confidence/reasoning pairs starting at index 19.
# Each tuple is (confidence_idx, reasoning_idx).
EXTENDED_COLUMNS: dict[str, tuple[int, int]] = {
    "greeting": (19, 20),
    "identity_validation": (21, 22),
    "purpose_of_call": (23, 24),
    "matching_the_moment": (25, 26),
    "process_adherence": (27, 28),
    "call_resolution": (29, 30),
    "communication": (31, 32),
    "efficiency": (33, 34),
    "customer_resolution_indicator": (35, 36),
}

# New columns after the extended pairs (added by DataPoints feature)
COL_CALL_SUMMARY = 37
COL_CALLER_NAME = 38
COL_CALLER_PHONE = 39

# ---------------------------------------------------------------------------
# Timestamp parsing
# ---------------------------------------------------------------------------
_TS_FORMATS = [
    "%m/%d/%Y %H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%m/%d/%Y %H:%M",
    "%Y-%m-%dT%H:%M:%S",
]


def _parse_timestamp(value: str) -> datetime | None:
    """Try several common timestamp formats. Return None if none match."""
    for fmt in _TS_FORMATS:
        try:
            return datetime.strptime(value.strip(), fmt)
        except (ValueError, AttributeError):
            continue
    return None


# ---------------------------------------------------------------------------
# Row parser
# ---------------------------------------------------------------------------

def _safe(row: list, idx: int, default: str = "") -> str:
    """Return row[idx] if it exists, else *default*."""
    return row[idx] if idx < len(row) else default


def _extract_eval_id(dialpad_link: str) -> str:
    """Extract the call_id from a Dialpad link URL."""
    if not dialpad_link:
        return ""
    clean = dialpad_link.split("[")[0].strip()  # strip [LONG CALL] suffix
    return clean.rstrip("/").split("/")[-1]


def _parse_row(row: list[str]) -> EvaluationRecord | None:
    """Convert a single spreadsheet row into an EvaluationRecord, or None."""
    if len(row) < 15:
        return None

    ts = _parse_timestamp(_safe(row, COL_TIMESTAMP))
    if ts is None:
        return None

    # Build sections dict --------------------------------------------------
    sections: dict[str, SectionScore] = {}

    # Scored sections (1-5)
    for name, idx in SECTION_COLUMNS.items():
        score_val = str(_safe(row, idx))
        conf = None
        reasoning = None
        if name in EXTENDED_COLUMNS:
            ci, ri = EXTENDED_COLUMNS[name]
            conf = _safe(row, ci) or None
            reasoning = _safe(row, ri) or None
        sections[name] = SectionScore(
            score=score_val,
            confidence=conf,
            reasoning=reasoning,
        )

    # Y/N indicator sections
    for name, idx in YN_COLUMNS.items():
        yn_val = _safe(row, idx)
        conf = None
        reasoning = None
        if name in EXTENDED_COLUMNS:
            ci, ri = EXTENDED_COLUMNS[name]
            conf = _safe(row, ci) or None
            reasoning = _safe(row, ri) or None
        sections[name] = SectionScore(
            score=yn_val,
            confidence=conf,
            reasoning=reasoning,
        )

    # Overall score --------------------------------------------------------
    try:
        overall = float(_safe(row, COL_OVERALL_SCORE, "0"))
    except (ValueError, TypeError):
        overall = 0.0

    dialpad_link = _safe(row, COL_DIALPAD_LINK) or None

    return EvaluationRecord(
        timestamp=ts,
        agent_name=_safe(row, COL_AGENT_NAME),
        agent_email=_safe(row, COL_AGENT_EMAIL),
        manager_email=_safe(row, COL_MANAGER_EMAIL),
        overall_score=overall,
        sections=sections,
        eval_id=_extract_eval_id(dialpad_link or ""),
        dialpad_link=dialpad_link,
        key_strengths=_safe(row, COL_KEY_STRENGTHS) or None,
        improvements=_safe(row, COL_IMPROVEMENTS) or None,
        call_summary=_safe(row, COL_CALL_SUMMARY) or None,
        caller_name=_safe(row, COL_CALLER_NAME) or None,
        caller_phone=_safe(row, COL_CALLER_PHONE) or None,
        source=_safe(row, COL_SOURCE) or "manual",
    )


# ---------------------------------------------------------------------------
# SheetsProvider
# ---------------------------------------------------------------------------

class SheetsProvider(DataProvider):
    """Reads evaluation data from the Analyst_History Google Sheet."""

    name = "Google Sheets"

    def __init__(self) -> None:
        import json as _json
        creds_env = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")

        # Support file path (local dev) or inline JSON (Railway/production)
        if creds_env.strip().startswith("{"):
            creds_info = _json.loads(creds_env)
            creds = Credentials.from_service_account_info(creds_info, scopes=_SCOPES)
        else:
            creds = Credentials.from_service_account_file(creds_env, scopes=_SCOPES)

        self._gc = gspread.authorize(creds)
        sheet_id = os.environ["GOOGLE_SHEETS_ID"]
        tab_name = os.environ.get("GOOGLE_HISTORY_TAB", "Analyst_History")
        self._ws = self._gc.open_by_key(sheet_id).worksheet(tab_name)

    # ------------------------------------------------------------------
    async def list_agents(self) -> list[str]:
        """Return sorted, deduplicated agent names from column A."""
        cached = _get_cached_raw("history")
        if cached is not None:
            values = [row[0] if row else "" for row in cached]
        else:
            values = self._ws.col_values(1)  # 1-indexed in gspread
        # Skip header row
        names = sorted(set(v.strip() for v in values[1:] if v.strip()))
        return names

    # ------------------------------------------------------------------
    async def get_agent_history(
        self, agent_name: str, days: int = 30
    ) -> list[EvaluationRecord]:
        """Return evaluations for *agent_name* within the last *days* days."""
        cutoff = datetime.now() - timedelta(days=days)
        cached = _get_cached_raw("history")
        if cached is not None:
            all_rows = cached
        else:
            all_rows = self._ws.get_all_values()
            _set_cached_raw("history", all_rows)

        records: list[EvaluationRecord] = []
        for row in all_rows[1:]:  # skip header
            if not row:
                continue
            if row[COL_AGENT_NAME].strip().lower() != agent_name.strip().lower():
                continue
            rec = _parse_row(row)
            if rec is None:
                continue
            if rec.timestamp < cutoff:
                continue
            records.append(rec)

        records.sort(key=lambda r: r.timestamp)
        return records

    # ------------------------------------------------------------------
    def _get_mails_sheet(self) -> list[list[str]]:
        """Read the Mails tab (agent roster with supervisors and canonical names).
        Returns raw rows including header. Cached for 5 minutes."""
        cached = _get_cached_raw("mails")
        if cached is not None:
            return cached
        mails_ws = self._gc.open_by_key(
            os.environ.get("GOOGLE_SHEETS_ID", "")
        ).worksheet("Mails")
        data = mails_ws.get_all_values()
        _set_cached_raw("mails", data)
        return data

    # ------------------------------------------------------------------
    async def get_all_history(self, days: int = 90) -> list[EvaluationRecord]:
        """Return ALL evaluation records within the time window (no agent filter)."""
        cached = _get_cached_raw("history")
        if cached is not None:
            all_rows = cached
        else:
            all_rows = self._ws.get_all_values()
            _set_cached_raw("history", all_rows)

        cutoff = datetime.now() - timedelta(days=days)
        records: list[EvaluationRecord] = []
        for row in all_rows[1:]:  # skip header
            if not row:
                continue
            rec = _parse_row(row)
            if rec is None:
                continue
            if rec.timestamp < cutoff:
                continue
            records.append(rec)
        records.sort(key=lambda r: r.timestamp)
        return records

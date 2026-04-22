"""Google Sheets data provider — reads Analyst_History tab.

Column layout is read from TeamConfig rather than hardcoded constants.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import time as _time

import gspread
from google.oauth2.service_account import Credentials

from backend.config.env import env_for_team
from backend.models.dashboard import EvaluationRecord, SectionScore
from backend.services.data_provider import DataProvider

if TYPE_CHECKING:
    from backend.config.team_config import AnalystHistoryConfig, TeamConfig

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
# Helpers
# ---------------------------------------------------------------------------

def _safe(row: list, idx: int, default: str = "") -> str:
    """Return row[idx] if it exists, else *default*."""
    return row[idx] if idx < len(row) else default


def _extract_eval_id(dialpad_link: str) -> str:
    """Extract the call_id from a Dialpad link URL.

    Handles variations:
      https://dialpad.com/callhistory/callreview/5644687275335680
      https://dialpad.com/callhistory/callreview/5644687275335680?source=session-history...
      https://dialpad.com/callhistory/callreview/5644687275335680 [LONG CALL — ...]
    """
    if not dialpad_link:
        return ""
    clean = dialpad_link.split("[")[0].strip()  # strip [LONG CALL] suffix
    clean = clean.split("?")[0].strip()         # strip query parameters
    return clean.rstrip("/").split("/")[-1]


# ---------------------------------------------------------------------------
# SheetsProvider
# ---------------------------------------------------------------------------

class SheetsProvider(DataProvider):
    """Reads evaluation data from the Analyst_History Google Sheet."""

    name = "Google Sheets"

    def __init__(self, config: TeamConfig | None = None) -> None:
        import json as _json

        # Load config if not provided
        if config is None:
            from backend.config.team_config import get_team_config
            config = get_team_config()
        self._config = config
        self._ah = config.sheets.analyst_history

        creds_env = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")

        # Support file path (local dev) or inline JSON (Railway/production)
        if creds_env.strip().startswith("{"):
            creds_info = _json.loads(creds_env)
            creds = Credentials.from_service_account_info(creds_info, scopes=_SCOPES)
        else:
            creds = Credentials.from_service_account_file(creds_env, scopes=_SCOPES)

        self._gc = gspread.authorize(creds)
        sheet_id = env_for_team("GOOGLE_SHEETS_ID", config.team_id, legacy_ok=True)
        if not sheet_id:
            raise RuntimeError(
                f"GOOGLE_SHEETS_ID (or GOOGLE_SHEETS_ID_{config.team_id.upper()}) not set"
            )
        self._sheet_id = sheet_id
        tab_name = (
            env_for_team("GOOGLE_HISTORY_TAB", config.team_id, legacy_ok=True)
            or self._ah.tab_name_default
        )
        self._ws = self._gc.open_by_key(sheet_id).worksheet(tab_name)

    # ------------------------------------------------------------------
    # Row parser (reads column layout from self._ah)
    # ------------------------------------------------------------------

    def _parse_row(self, row: list[str]) -> EvaluationRecord | None:
        """Convert a single spreadsheet row into an EvaluationRecord, or None."""
        ah = self._ah

        if len(row) < 15:
            return None

        ts = _parse_timestamp(_safe(row, ah.col_timestamp))
        if ts is None:
            return None

        # Build sections dict ------------------------------------------------
        sections: dict[str, SectionScore] = {}

        # Scored sections (1-5)
        for name, idx in ah.section_columns.items():
            score_val = str(_safe(row, idx))
            conf = None
            reasoning = None
            if name in ah.extended_columns:
                ci, ri = ah.extended_columns[name]
                conf = _safe(row, ci) or None
                reasoning = _safe(row, ri) or None
            sections[name] = SectionScore(
                score=score_val,
                confidence=conf,
                reasoning=reasoning,
            )

        # Y/N indicator sections
        for name, idx in ah.yn_columns.items():
            yn_val = _safe(row, idx)
            conf = None
            reasoning = None
            if name in ah.extended_columns:
                ci, ri = ah.extended_columns[name]
                conf = _safe(row, ci) or None
                reasoning = _safe(row, ri) or None
            sections[name] = SectionScore(
                score=yn_val,
                confidence=conf,
                reasoning=reasoning,
            )

        # Overall score ------------------------------------------------------
        try:
            overall = float(_safe(row, ah.col_overall_score, "0"))
        except (ValueError, TypeError):
            overall = 0.0

        dialpad_link = _safe(row, ah.col_dialpad_link) or None

        return EvaluationRecord(
            timestamp=ts,
            agent_name=_safe(row, ah.col_agent_name),
            agent_email=_safe(row, ah.col_agent_email),
            manager_email=_safe(row, ah.col_manager_email),
            overall_score=overall,
            sections=sections,
            eval_id=_extract_eval_id(dialpad_link or ""),
            dialpad_link=dialpad_link,
            key_strengths=_safe(row, ah.col_key_strengths) or None,
            improvements=_safe(row, ah.col_improvements) or None,
            call_summary=_safe(row, ah.col_call_summary) or None,
            caller_name=_safe(row, ah.col_caller_name) or None,
            caller_phone=_safe(row, ah.col_caller_phone) or None,
            source=_safe(row, ah.col_source) or "manual",
        )

    # ------------------------------------------------------------------
    @property
    def _history_cache_key(self) -> str:
        return f"{self._config.team_id}:history"

    @property
    def _mails_cache_key(self) -> str:
        return f"{self._config.team_id}:mails"

    async def list_agents(self) -> list[str]:
        """Return sorted, deduplicated agent names from column A."""
        cached = _get_cached_raw(self._history_cache_key)
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
        cached = _get_cached_raw(self._history_cache_key)
        if cached is not None:
            all_rows = cached
        else:
            all_rows = self._ws.get_all_values()
            _set_cached_raw(self._history_cache_key, all_rows)

        records: list[EvaluationRecord] = []
        for row in all_rows[1:]:  # skip header
            if not row:
                continue
            if row[self._ah.col_agent_name].strip().lower() != agent_name.strip().lower():
                continue
            rec = self._parse_row(row)
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
        cached = _get_cached_raw(self._mails_cache_key)
        if cached is not None:
            return cached
        mails_ws = self._gc.open_by_key(self._sheet_id).worksheet(
            self._config.sheets.mails_tab
        )
        data = mails_ws.get_all_values()
        _set_cached_raw(self._mails_cache_key, data)
        return data

    # ------------------------------------------------------------------
    async def get_all_history(self, days: int = 90) -> list[EvaluationRecord]:
        """Return ALL evaluation records within the time window (no agent filter)."""
        cached = _get_cached_raw(self._history_cache_key)
        if cached is not None:
            all_rows = cached
        else:
            all_rows = self._ws.get_all_values()
            _set_cached_raw(self._history_cache_key, all_rows)

        cutoff = datetime.now() - timedelta(days=days)
        records: list[EvaluationRecord] = []
        for row in all_rows[1:]:  # skip header
            if not row:
                continue
            rec = self._parse_row(row)
            if rec is None:
                continue
            if rec.timestamp < cutoff:
                continue
            records.append(rec)
        records.sort(key=lambda r: r.timestamp)
        return records

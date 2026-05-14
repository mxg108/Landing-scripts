"""Google Sheets data provider — reads Analyst_History tab.

Schema v2.0 (Phase 2): Column positions are derived from
``config.history_layout`` (no per-team col_* fields). Section scores,
reasoning, and confidence sit in three contiguous ranges of length N
in section_number order; metadata lives in fixed prefix/trailing
columns.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import time as _time

import gspread
from google.oauth2.service_account import Credentials

from backend.config import history_layout
from backend.config.env import env_for_team
from backend.models.dashboard import EvaluationRecord, SectionScore
from backend.services.data_provider import DataProvider

if TYPE_CHECKING:
    from backend.config.team_config import TeamConfig

# ---------------------------------------------------------------------------
# Raw sheet data cache with 5-minute TTL
# ---------------------------------------------------------------------------
_sheet_cache: dict[str, tuple[float, list[list[str]]]] = {}
_CACHE_TTL = 300  # 5 minutes


def _get_cached_raw(key: str) -> list[list[str]] | None:
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

# Timestamp parsing — delegate to the canonical parser in
# data_normalization so all readers (team_stats, history_service)
# accept the same format set. A previous local copy of _TS_FORMATS
# diverged and dropped Sales' date-only rows from per-agent views
# while team-level views worked.
from backend.services.data_normalization import parse_timestamp as _parse_timestamp  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe(row: list, idx: int, default: str = "") -> str:
    return row[idx] if idx < len(row) else default


def _extract_eval_id(dialpad_link: str) -> str:
    """Extract the call_id from a Dialpad link URL."""
    if not dialpad_link:
        return ""
    clean = dialpad_link.split("[")[0].strip()
    clean = clean.split("?")[0].strip()
    return clean.rstrip("/").split("/")[-1]


# ---------------------------------------------------------------------------
# SheetsProvider
# ---------------------------------------------------------------------------

class SheetsProvider(DataProvider):
    """Reads evaluation data from the Analyst_History Google Sheet."""

    name = "Google Sheets"

    def __init__(self, config: TeamConfig | None = None) -> None:
        import json as _json

        if config is None:
            from backend.config.team_config import get_team_config
            config = get_team_config()
        self._config = config
        self._layout = config.history_layout
        # Pre-compute (history_id -> col_index) so _parse_row stays fast.
        self._section_col_by_id = {
            s.history_id: self._layout.col_score(i)
            for i, s in enumerate(config.sections_by_number)
        }
        self._section_reasoning_col = {
            s.history_id: self._layout.col_reasoning(i)
            for i, s in enumerate(config.sections_by_number)
        }
        self._section_confidence_col = {
            s.history_id: self._layout.col_confidence(i)
            for i, s in enumerate(config.sections_by_number)
        }

        creds_env = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")

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
            or config.sheets.analyst_history.tab_name
        )
        self._ws = self._gc.open_by_key(sheet_id).worksheet(tab_name)

    # ------------------------------------------------------------------
    # Row parser (reads column layout from config.history_layout)
    # ------------------------------------------------------------------

    def _parse_row(self, row: list[str]) -> EvaluationRecord | None:
        """Convert a single Analyst_History row into an EvaluationRecord, or None.

        Minimum row width is the prefix (cols A-F = 6 cells) so we can
        identify the eval and read its overall_score. Anything narrower
        is rejected as malformed.
        """
        if len(row) < history_layout.COL_OVERALL_SCORE + 1:
            return None

        ts = _parse_timestamp(_safe(row, history_layout.COL_TIMESTAMP))
        if ts is None:
            return None

        # Build sections dict keyed by history_id (preserves the existing
        # API contract).
        sections: dict[str, SectionScore] = {}
        for sec in self._config.sections_by_number:
            score_idx = self._section_col_by_id[sec.history_id]
            reasoning_idx = self._section_reasoning_col[sec.history_id]
            confidence_idx = self._section_confidence_col[sec.history_id]

            score_val = str(_safe(row, score_idx))
            reasoning = _safe(row, reasoning_idx) or None
            confidence = _safe(row, confidence_idx) or None

            sections[sec.history_id] = SectionScore(
                score=score_val,
                confidence=confidence,
                reasoning=reasoning,
            )

        try:
            overall = float(_safe(row, history_layout.COL_OVERALL_SCORE, "0"))
        except (ValueError, TypeError):
            overall = 0.0

        dialpad_link = _safe(row, history_layout.COL_DIALPAD_LINK) or None
        L = self._layout

        return EvaluationRecord(
            timestamp=ts,
            agent_name=_safe(row, history_layout.COL_AGENT_NAME),
            agent_email=_safe(row, history_layout.COL_AGENT_EMAIL),
            # The schema renamed "manager_email" → "evaluator_email"; the
            # EvaluationRecord field name stays as manager_email for
            # downstream compatibility (frontend reads r.manager_email).
            manager_email=_safe(row, history_layout.COL_EVALUATOR_EMAIL),
            overall_score=overall,
            sections=sections,
            eval_id=_extract_eval_id(dialpad_link or ""),
            dialpad_link=dialpad_link,
            key_strengths=_safe(row, L.col_key_strengths) or None,
            # Same naming bridge: model field is "improvements", new
            # schema cell is "opportunities". Field name standardization
            # is deferred to Phase D (frontend rename).
            improvements=_safe(row, L.col_opportunities) or None,
            call_summary=_safe(row, L.col_call_summary) or None,
            caller_name=_safe(row, L.col_caller_name) or None,
            caller_phone=_safe(row, L.col_caller_phone) or None,
            source=_safe(row, L.col_source) or "manual",
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
            if _safe(row, history_layout.COL_AGENT_NAME).strip().lower() != agent_name.strip().lower():
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
        """Read the Mails tab (agent roster). Cached for 5 minutes."""
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
        """Return ALL evaluation records within the time window."""
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

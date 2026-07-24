"""PostgreSQL data provider — the qa.* read path for the post-backfill flip.

Mirrors SheetsProvider's EvaluationRecord contract exactly (sections keyed
by history_id, the manager_email/improvements naming bridges, tz-aware UTC
timestamps) so the read-path flip is a provider swap inside
``data_provider.get_provider``, not a shape change. Until that flip, the
factory still returns SheetsProvider and nothing instantiates this class.

Row membership matches Analyst_History: only ``state='finalized'``
evaluations exist in the sheet (Stage 4 writes on finalize), so only those
are served here.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone

from backend.config.team_config import TeamConfig
from backend.models.dashboard import EvaluationRecord, SectionScore
from backend.services.data_provider import DataProvider
from backend.services.history_service import _extract_eval_id
from backend.services.sheets_service import YN_DISPLAY

# Same 5-minute freshness window as history_service's sheet cache.
_ROSTER_TTL_SECONDS = 300

_EVAL_COLUMNS = (
    "id, agent_name_raw, agent_email, evaluator_email, "
    "COALESCE(call_connected_at, created_at) AS ts, "
    "overall_score, dialpad_link, key_strengths, opportunities, "
    "call_summary, caller_name, caller_phone, source, "
    # Call metadata for the /datapoint drill-down (DispositionDesign §5,
    # PulpoConnection §4.2): CC stamps, clocks, the id triple, and the
    # metadata JSONB carrying sop_used + pulpo_docs provenance.
    "dialpad_disposition_category, dialpad_disposition, ai_csat, "
    "call_duration_ms, dialpad_call_id, dialpad_entry_point_call_id, "
    "dialpad_master_call_id, dialpad_call_metadata"
)


def _display_score(section_row) -> str:
    """Render a qa.evaluation_sections row the way the sheet cell reads.

    The Stage-4 projection writes numeric scores as digits and binary/NA
    via YN_DISPLAY; matching that here keeps SectionScore.score strings
    identical across providers.
    """
    if section_row is None:
        return ""
    if section_row["numeric_score"] is not None:
        return str(section_row["numeric_score"])
    if section_row["binary_value"] is not None:
        return YN_DISPLAY.get(section_row["binary_value"], "Not Applicable")
    return ""


class PostgresProvider(DataProvider):
    """Reads evaluation data from the PostgreSQL ``qa`` schema."""

    name = "PostgreSQL"

    def __init__(self, config: TeamConfig | None = None):
        if config is None:
            from backend.config.team_config import get_team_config
            config = get_team_config()
        self._config = config
        self._team_id = config.team_id
        self._pool = None
        self._dsn = os.environ.get("DATABASE_URL", "")
        # Mails-shaped roster snapshot from qa.agents; _get_mails_sheet is
        # sync (interface parity with SheetsProvider) so it serves this
        # snapshot, refreshed by the async entry points when stale.
        self._roster: list[list[str]] | None = None
        self._roster_at = 0.0

    async def connect(self):
        import asyncpg
        if not self._dsn:
            raise ConnectionError("DATABASE_URL not configured")
        self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=5, timeout=3)
        async with self._pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        await self._refresh_roster()

    async def close(self):
        if self._pool:
            await self._pool.close()

    # ------------------------------------------------------------------
    async def list_agents(self) -> list[str]:
        await self._refresh_roster_if_stale()
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT DISTINCT agent_name_raw FROM qa.evaluations "
                "WHERE team_id = $1 AND state = 'finalized' "
                "ORDER BY agent_name_raw",
                self._team_id,
            )
        return [r["agent_name_raw"].strip() for r in rows if r["agent_name_raw"].strip()]

    async def get_agent_history(self, agent_name: str, days: int = 30) -> list[EvaluationRecord]:
        return await self._fetch_records(days=days, agent_name=agent_name)

    async def get_agent_history_range(
        self, agent_name: str, start: datetime, end: datetime
    ) -> list[EvaluationRecord]:
        """Explicit [start, end) window — the EOM one-pager's calendar-month
        fetch (JulyR2R3 §3); the rolling `days` cutoff can't express a
        closed month."""
        await self._refresh_roster_if_stale()
        async with self._pool.acquire() as conn:
            evals = await conn.fetch(
                f"SELECT {_EVAL_COLUMNS} FROM qa.evaluations "
                "WHERE team_id = $1 AND state = 'finalized' "
                "AND LOWER(agent_name_raw) = LOWER($2) "
                "AND COALESCE(call_connected_at, created_at) >= $3 "
                "AND COALESCE(call_connected_at, created_at) < $4 "
                "ORDER BY COALESCE(call_connected_at, created_at)",
                self._team_id, agent_name.strip(), start, end,
            )
            if not evals:
                return []
            sections = await conn.fetch(
                "SELECT evaluation_id, section_id, numeric_score, binary_value, "
                "confidence, reasoning "
                "FROM qa.evaluation_sections WHERE evaluation_id = ANY($1::bigint[])",
                [ev["id"] for ev in evals],
            )
        sections_by_eval: dict[int, list] = {}
        for s in sections:
            sections_by_eval.setdefault(s["evaluation_id"], []).append(s)
        return [
            self._record_from_rows(ev, sections_by_eval.get(ev["id"], []))
            for ev in evals
        ]

    async def get_all_history(self, days: int = 90) -> list[EvaluationRecord]:
        return await self._fetch_records(days=days)

    async def get_by_eval_id(self, call_id: str) -> EvaluationRecord | None:
        """Indexed single-eval lookup for /datapoints/{call_id} (W6).

        Fast path: probe the Dialpad id columns — the entry-point id is the
        link's trailing segment in the common case (idx_eval_entry_point_
        call_id, migration 014); dialpad_call_id covers the rest
        (uq_eval_team_call_id). Replaces the 365-day get_all_history load +
        Python scan with a single indexed row hit.

        Returns None on a miss OR when the resolved row's link-derived
        eval_id disagrees with call_id, so the route falls back to the scan
        (junk-link rows whose stored ids don't match the link) — the result
        is never a different record than the scan would have returned.
        """
        if not call_id:
            return None
        await self._refresh_roster_if_stale()
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {_EVAL_COLUMNS} FROM qa.evaluations "
                "WHERE team_id = $1 AND state = 'finalized' "
                "AND (dialpad_entry_point_call_id = $2 OR dialpad_call_id = $2) "
                # Match get_all_history's ascending order so a (rare)
                # duplicate call_id resolves to the same row the scan picks.
                "ORDER BY COALESCE(call_connected_at, created_at) LIMIT 1",
                self._team_id, call_id,
            )
            if row is None:
                return None
            sections = await conn.fetch(
                "SELECT evaluation_id, section_id, numeric_score, binary_value, "
                "confidence, reasoning "
                "FROM qa.evaluation_sections WHERE evaluation_id = $1",
                row["id"],
            )
        record = self._record_from_rows(row, sections)
        return record if record.eval_id == call_id else None

    # ------------------------------------------------------------------
    async def _fetch_records(
        self, days: int, agent_name: str | None = None
    ) -> list[EvaluationRecord]:
        await self._refresh_roster_if_stale()
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        query = (
            f"SELECT {_EVAL_COLUMNS} FROM qa.evaluations "
            "WHERE team_id = $1 AND state = 'finalized' "
            "AND COALESCE(call_connected_at, created_at) >= $2 "
        )
        params: list = [self._team_id, cutoff]
        if agent_name is not None:
            query += "AND LOWER(agent_name_raw) = LOWER($3) "
            params.append(agent_name.strip())
        query += "ORDER BY COALESCE(call_connected_at, created_at)"

        async with self._pool.acquire() as conn:
            evals = await conn.fetch(query, *params)
            if not evals:
                return []
            sections = await conn.fetch(
                "SELECT evaluation_id, section_id, numeric_score, binary_value, "
                "confidence, reasoning "
                "FROM qa.evaluation_sections WHERE evaluation_id = ANY($1::bigint[])",
                [ev["id"] for ev in evals],
            )

        sections_by_eval: dict[int, list] = {}
        for s in sections:
            sections_by_eval.setdefault(s["evaluation_id"], []).append(s)
        return [
            self._record_from_rows(ev, sections_by_eval.get(ev["id"], []))
            for ev in evals
        ]

    def _record_from_rows(self, ev, section_rows) -> EvaluationRecord:
        """Build an EvaluationRecord shaped exactly like SheetsProvider._parse_row."""
        by_section_id = {s["section_id"]: s for s in section_rows}
        # Every configured section appears in the dict, blank when the
        # evaluation predates it — same as reading an empty sheet cell.
        # DB rows for sections no longer in the active rubric are dropped,
        # matching the sheet reader which only looks at configured columns.
        sections: dict[str, SectionScore] = {}
        for sec in self._config.sections_by_number:
            row = by_section_id.get(sec.id)
            sections[sec.history_id] = SectionScore(
                score=_display_score(row),
                confidence=row["confidence"] if row else None,
                reasoning=row["reasoning"] if row else None,
            )

        ts = ev["ts"]
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)

        dialpad_link = ev["dialpad_link"] or None
        # dialpad_call_metadata is JSONB; asyncpg returns it as a JSON
        # string without a custom codec. Tolerate str, dict, or NULL —
        # `.get` works on both asyncpg Records and dict test fixtures.
        metadata = ev.get("dialpad_call_metadata")
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except ValueError:
                metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        pulpo_docs = metadata.get("pulpo_docs")
        return EvaluationRecord(
            timestamp=ts,
            agent_name=ev["agent_name_raw"],
            agent_email=ev["agent_email"] or "",
            # Naming bridge (see SheetsProvider._parse_row): the schema
            # renamed manager_email → evaluator_email; the record field
            # stays manager_email for downstream compatibility.
            manager_email=ev["evaluator_email"] or "",
            overall_score=float(ev["overall_score"]) if ev["overall_score"] is not None else 0.0,
            sections=sections,
            eval_id=_extract_eval_id(dialpad_link or ""),
            dialpad_link=dialpad_link,
            key_strengths=ev["key_strengths"] or None,
            # Same bridge: schema cell is "opportunities", record field is
            # "improvements" until the Phase D frontend rename.
            improvements=ev["opportunities"] or None,
            call_summary=ev["call_summary"] or None,
            caller_name=ev["caller_name"] or None,
            caller_phone=ev["caller_phone"] or None,
            source=ev["source"] or "manual",
            dialpad_disposition_category=ev.get("dialpad_disposition_category") or None,
            dialpad_disposition=ev.get("dialpad_disposition") or None,
            ai_csat=float(ev["ai_csat"]) if ev.get("ai_csat") is not None else None,
            call_duration_ms=ev.get("call_duration_ms"),
            dialpad_call_id=ev.get("dialpad_call_id") or None,
            dialpad_entry_point_call_id=ev.get("dialpad_entry_point_call_id") or None,
            dialpad_master_call_id=ev.get("dialpad_master_call_id") or None,
            sop_used=metadata.get("sop_used") or None,
            pulpo_docs=pulpo_docs if isinstance(pulpo_docs, list) else [],
        )

    # ------------------------------------------------------------------
    # Roster (qa.agents replaces the Mails tab — CutoverDesign §2)
    # ------------------------------------------------------------------
    async def _refresh_roster(self) -> None:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT name, email, supervisor_email, canonical_name "
                "FROM qa.agents WHERE team_id = $1 AND active ORDER BY name",
                self._team_id,
            )
        self._roster = [["Name", "Email", "Supervisor", "Canonical Name"]] + [
            [r["name"], r["email"], r["supervisor_email"] or "", r["canonical_name"] or ""]
            for r in rows
        ]
        self._roster_at = time.monotonic()

    async def _refresh_roster_if_stale(self) -> None:
        if self._roster is not None and time.monotonic() - self._roster_at < _ROSTER_TTL_SECONDS:
            return
        await self._refresh_roster()

    def _get_mails_sheet(self) -> list[list[str]]:
        """Mails-shaped roster rows (A=Name, B=Email, C=Supervisor,
        D=Canonical Name, header included) served from the qa.agents
        snapshot so history_service's roster helpers work unchanged."""
        if self._roster is None:
            raise RuntimeError("PostgresProvider roster not loaded — call connect() first")
        return self._roster

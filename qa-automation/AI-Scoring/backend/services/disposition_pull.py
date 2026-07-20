"""Stats-API dispositions pull — the interim CC v1 ingestion path.

With the webhook receiver blocked (Dialpad subscription cap + Railway
root-directory change), dispositions arrive backfill-FIRST: a Stats
`dispositions` records export per call center, pulled on a loop with
`is_today` so today's calls land within the half-hour, then filled into
`command_center.calls` (UPSERT — rows are CREATED here, seen_via
'stats_pull', so the C3 grounding match works without webhooks) and
`qa.evaluations` (late-scored calls whose eval predates the pull).

Idempotency is structural, not diff-based: INSERT ... ON CONFLICT with a
`disposition_source IS NULL` guard makes every re-pull a no-op except
for genuinely new calls and late-selected dispositions — a webhook-era
stamp always wins the seam once the receiver goes live.

Runs three ways:
- `run_periodic_pull()` — in-app asyncio loop, started by the FastAPI
  lifespan when CC_STATS_PULL_INTERVAL_MIN is set (Railway env).
- `pull_once(...)` — one fetch+fill cycle (the loop body).
- scripts/pull_dispositions.py — thin CLI wrapper for manual ranges,
  offline CSVs, and dry runs.

Env:
  DIALPAD_API_KEY               Stats API auth (existing)
  CC_STATS_PULL_INTERVAL_MIN    loop interval; unset → loop disabled
  CC_STATS_PULL_TARGETS         comma-separated call-center ids
                                (default: MS 5699048497577984)
  CC_STATS_PULL_TEAM            team_id (default member_support)
  CC_STATS_PULL_TZ              Stats-request tz (default America/Mexico_City)
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import httpx

from backend.services.eval_store import get_pool

logger = logging.getLogger(__name__)

BASE_URL = "https://dialpad.com/api/v2"

DEFAULT_TARGETS = "5699048497577984"
DEFAULT_TEAM = "member_support"
DEFAULT_TZ = "America/Mexico_City"

POLL_INTERVAL_S = 5
POLL_TIMEOUT_S = 600


# ---------------------------------------------------------------------------
# Pure helpers — testable without API or DB
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StatsRecord:
    call_id: str
    disposition_category: Optional[str]
    disposition: Optional[str]
    # Call metadata riding the export — populates the CREATED calls row.
    direction: Optional[str] = None
    external_number: Optional[str] = None
    internal_number: Optional[str] = None
    agent_name: Optional[str] = None
    started_at: Optional[datetime] = None
    connected_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None


def split_disposition(label: str) -> tuple[Optional[str], Optional[str]]:
    """`Category~Subdisposition` → pair; bare category → (category, None)."""
    label = (label or "").strip()
    if not label:
        return (None, None)
    category, _, sub = label.partition("~")
    return (category.strip(), sub.strip() or None)


def _row_ts(value: str, tz_name: str) -> Optional[datetime]:
    """Export timestamps are NAIVE in the row's own timezone column —
    localize to UTC-aware; None on junk."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        naive = datetime.fromisoformat(value)
        return naive.replace(tzinfo=ZoneInfo(tz_name or "UTC"))
    except (ValueError, KeyError):
        return None


def parse_export_csv(text: str) -> list[StatsRecord]:
    """Rows of the dispositions records export. Rows without a call_id
    drop (unjoinable); rows without a disposition are KEPT — they seed a
    calls row whose NULL disposition a later pull fills in place, and
    they are the coverage denominator (absence is expected-normal)."""
    records = []
    for row in csv.DictReader(io.StringIO(text)):
        call_id = (row.get("call_id") or "").strip()
        if not call_id:
            continue
        category, sub = split_disposition(row.get("disposition") or "")
        tz = (row.get("timezone") or "").strip()
        records.append(StatsRecord(
            call_id=call_id,
            disposition_category=category,
            disposition=sub,
            direction=(row.get("direction") or "").strip() or None,
            external_number=(row.get("external_number") or "").strip() or None,
            internal_number=(row.get("internal_number") or "").strip() or None,
            agent_name=(row.get("operator_name") or "").strip() or None,
            started_at=_row_ts(row.get("date_started", ""), tz),
            connected_at=_row_ts(row.get("date_connected", ""), tz),
            ended_at=_row_ts(row.get("date_ended", ""), tz),
        ))
    return records


def pull_targets() -> list[str]:
    raw = os.environ.get("CC_STATS_PULL_TARGETS", DEFAULT_TARGETS)
    return [t.strip() for t in raw.split(",") if t.strip()]


def pull_team() -> str:
    return os.environ.get("CC_STATS_PULL_TEAM", DEFAULT_TEAM)


# ---------------------------------------------------------------------------
# Stats API — initiate / poll / download (async)
# ---------------------------------------------------------------------------


async def fetch_export(
    call_center_id: str,
    *,
    days_start: int = 1,
    days_end: int = 20,
    is_today: bool = False,
) -> str:
    """Initiate a dispositions records export, poll, download the CSV.

    `is_today=True` scopes the export to today's calls (the loop's mode);
    otherwise `days_ago_start`/`days_ago_end` bound the range. Raises on
    auth/HTTP/timeout — callers own the retry/logging policy.
    """
    api_key = os.environ.get("DIALPAD_API_KEY", "")
    if not api_key:
        raise RuntimeError("DIALPAD_API_KEY not set")
    headers = {"Authorization": f"Bearer {api_key}"}

    payload: dict = {
        "export_type": "records",
        "stat_type": "dispositions",
        "timezone": os.environ.get("CC_STATS_PULL_TZ", DEFAULT_TZ),
        "target_id": int(call_center_id),
        "target_type": "callcenter",
    }
    if is_today:
        payload["is_today"] = True
    else:
        payload["days_ago_start"] = days_start
        payload["days_ago_end"] = days_end

    async with httpx.AsyncClient(headers=headers, timeout=30) as client:
        resp = await client.post(f"{BASE_URL}/stats", json=payload)
        resp.raise_for_status()
        request_id = resp.json()["request_id"]
        logger.info(
            "disposition_pull: export initiated request_id=%s target=%s",
            request_id, call_center_id,
        )

        deadline = asyncio.get_event_loop().time() + POLL_TIMEOUT_S
        while True:
            status = await client.get(f"{BASE_URL}/stats/{request_id}")
            status.raise_for_status()
            body = status.json()
            if body.get("status") == "complete":
                url = body["download_url"]
                break
            if body.get("status") == "failed":
                raise RuntimeError(f"stats export failed: {body}")
            if asyncio.get_event_loop().time() > deadline:
                raise TimeoutError(
                    f"stats export {request_id} not complete after {POLL_TIMEOUT_S}s"
                )
            logger.info(
                "disposition_pull: export %s status=%s — polling",
                request_id, body.get("status"),
            )
            await asyncio.sleep(POLL_INTERVAL_S)

        download = await client.get(url, follow_redirects=True)
        download.raise_for_status()
        logger.info(
            "disposition_pull: export %s downloaded (%d bytes)",
            request_id, len(download.content),
        )
        return download.text


# ---------------------------------------------------------------------------
# DB fill — set-based UPSERT, structurally idempotent
# ---------------------------------------------------------------------------
#
# One statement per CHUNK of rows, not per row: the DB is on Railway, so
# per-row round trips over the WAN turned a 20-day backfill (~4-5k rows)
# into 15+ silent minutes. Arrays + unnest brings that to a handful of
# round trips. A chunk that fails falls back to per-row for isolation
# (the overnight contract: a bad row never kills the batch).

CHUNK_SIZE = 500

# The conditional DO UPDATE only fires when the existing row has no
# disposition yet (webhook-era stamps and earlier pulls win); the WHERE
# also stops rewriting identical NULL→NULL rows every half hour.
# RETURNING counts only rows actually inserted or updated.
_CALLS_UPSERT_BATCH = """
WITH data AS (
    SELECT * FROM unnest(
        $2::text[], $3::text[], $4::text[], $5::text[], $6::text[],
        $7::text[], $8::text[],
        $9::timestamptz[], $10::timestamptz[], $11::timestamptz[]
    ) AS d(call_id, cat, sub, direction, ext_num, int_num, agent,
           started, connected, ended)
),
written AS (
    INSERT INTO command_center.calls
        (team_id, dialpad_call_id, seen_via,
         disposition_category, disposition, disposition_source,
         direction, external_number, internal_number, agent_name,
         started_at, connected_at, ended_at)
    SELECT $1, call_id, 'stats_pull',
           cat, sub, CASE WHEN cat IS NULL THEN NULL ELSE 'stats_pull' END,
           direction, ext_num, int_num, agent, started, connected, ended
    FROM data
    ON CONFLICT ON CONSTRAINT uq_calls_team_call_id DO UPDATE SET
        disposition_category = EXCLUDED.disposition_category,
        disposition          = EXCLUDED.disposition,
        disposition_source   = EXCLUDED.disposition_source,
        last_updated_at      = NOW()
    WHERE command_center.calls.disposition_source IS NULL
      AND EXCLUDED.disposition_category IS NOT NULL
    RETURNING 1
)
SELECT count(*) FROM written
"""

# The export's call_id is the ENTRY-POINT id (verified live 2026-07-20).
# Historical evals stored only the per-leg id in dialpad_call_id and
# NULL in dialpad_entry_point_call_id — but dialpad_link embeds the
# entry-point id verbatim (build_dialpad_link prefers it), so the link
# equality is what actually joins the historic backfill. Newly scored
# evals carry the entry-point id in its own column.
_EVALS_FILL_BATCH = """
WITH data AS (
    SELECT * FROM unnest($2::text[], $3::text[], $4::text[])
        AS d(call_id, cat, sub)
)
UPDATE qa.evaluations e
SET dialpad_disposition_category = d.cat,
    dialpad_disposition = d.sub
FROM data d
WHERE e.team_id = $1 AND e.dialpad_disposition_category IS NULL
  AND (e.dialpad_call_id = d.call_id
       OR e.dialpad_entry_point_call_id = d.call_id
       OR e.dialpad_master_call_id = d.call_id
       OR e.dialpad_link =
          'https://dialpad.com/callhistory/callreview/' || d.call_id)
"""

# Dry-run: calls the export would write = not already carrying a
# disposition stamp (new rows + NULL-source rows).
_DRY_RUN_COUNT = """
SELECT count(*) FROM unnest($2::text[]) AS u(call_id)
WHERE NOT EXISTS (
    SELECT 1 FROM command_center.calls c
    WHERE c.team_id = $1 AND c.dialpad_call_id = u.call_id
      AND c.disposition_source IS NOT NULL
)
"""

_CALLS_UPSERT_ROW = """
INSERT INTO command_center.calls
    (team_id, dialpad_call_id, seen_via,
     disposition_category, disposition, disposition_source,
     direction, external_number, internal_number, agent_name,
     started_at, connected_at, ended_at)
VALUES ($1, $2, 'stats_pull',
        $3, $4, CASE WHEN $3::text IS NULL THEN NULL ELSE 'stats_pull' END,
        $5, $6, $7, $8, $9, $10, $11)
ON CONFLICT ON CONSTRAINT uq_calls_team_call_id DO UPDATE SET
    disposition_category = EXCLUDED.disposition_category,
    disposition          = EXCLUDED.disposition,
    disposition_source   = EXCLUDED.disposition_source,
    last_updated_at      = NOW()
WHERE command_center.calls.disposition_source IS NULL
  AND EXCLUDED.disposition_category IS NOT NULL
"""

_EVALS_FILL_ROW = """
UPDATE qa.evaluations
SET dialpad_disposition_category = $3,
    dialpad_disposition = $4
WHERE team_id = $1 AND dialpad_disposition_category IS NULL
  AND (dialpad_call_id = $2
       OR dialpad_entry_point_call_id = $2
       OR dialpad_master_call_id = $2
       OR dialpad_link =
          'https://dialpad.com/callhistory/callreview/' || $2)
"""


def dedupe_records(records: list[StatsRecord]) -> list[StatsRecord]:
    """One record per call_id (a multi-row upsert cannot touch the same
    row twice). Last wins — except a dispositioned record is never
    replaced by an undispositioned one."""
    by_id: dict[str, StatsRecord] = {}
    for record in records:
        prev = by_id.get(record.call_id)
        if prev is not None and prev.disposition_category and not record.disposition_category:
            continue
        by_id[record.call_id] = record
    return list(by_id.values())


def _chunks(items: list, size: int) -> list[list]:
    return [items[i:i + size] for i in range(0, len(items), size)]


async def _fill_chunk_per_row(conn, team_id: str, chunk: list[StatsRecord],
                              report: dict) -> None:
    """Fallback isolation path when a batch statement fails: replay the
    chunk row by row so one bad row costs one row, not five hundred."""
    for record in chunk:
        try:
            result = await conn.execute(
                _CALLS_UPSERT_ROW,
                team_id, record.call_id,
                record.disposition_category, record.disposition,
                record.direction, record.external_number,
                record.internal_number, record.agent_name,
                record.started_at, record.connected_at, record.ended_at,
            )
            report["calls_written"] += int(result.split()[-1])
            if record.disposition_category:
                ev = await conn.execute(
                    _EVALS_FILL_ROW,
                    team_id, record.call_id,
                    record.disposition_category, record.disposition,
                )
                report["evals_filled"] += int(ev.split()[-1])
        except Exception:
            report["row_failures"] += 1
            logger.exception(
                "disposition_pull: row failed call_id=%s", record.call_id
            )


async def fill_records(
    team_id: str, records: list[StatsRecord], *, dry_run: bool = False
) -> dict:
    """Fill both tables from parsed export records, CHUNK_SIZE rows per
    statement. A failing chunk falls back to per-row so a bad row never
    kills the batch."""
    records = dedupe_records(records)
    report = {
        "rows_in_export": len(records),
        "with_disposition": sum(1 for r in records if r.disposition_category),
        "calls_written": 0,
        "evals_filled": 0,
        "row_failures": 0,
    }
    pool = await get_pool()
    if pool is None:
        raise RuntimeError("DATABASE_URL not set — nothing to fill")

    chunks = _chunks(records, CHUNK_SIZE)
    async with pool.acquire() as conn:
        for i, chunk in enumerate(chunks, start=1):
            if dry_run:
                report["calls_written"] += await conn.fetchval(
                    _DRY_RUN_COUNT, team_id, [r.call_id for r in chunk],
                )
                continue
            try:
                written = await conn.fetchval(
                    _CALLS_UPSERT_BATCH,
                    team_id,
                    [r.call_id for r in chunk],
                    [r.disposition_category for r in chunk],
                    [r.disposition for r in chunk],
                    [r.direction for r in chunk],
                    [r.external_number for r in chunk],
                    [r.internal_number for r in chunk],
                    [r.agent_name for r in chunk],
                    [r.started_at for r in chunk],
                    [r.connected_at for r in chunk],
                    [r.ended_at for r in chunk],
                )
                report["calls_written"] += written
                dispositioned = [r for r in chunk if r.disposition_category]
                if dispositioned:
                    ev = await conn.execute(
                        _EVALS_FILL_BATCH,
                        team_id,
                        [r.call_id for r in dispositioned],
                        [r.disposition_category for r in dispositioned],
                        [r.disposition for r in dispositioned],
                    )
                    report["evals_filled"] += int(ev.split()[-1])
            except Exception:
                logger.exception(
                    "disposition_pull: chunk %d/%d batch failed — "
                    "replaying per-row", i, len(chunks),
                )
                await _fill_chunk_per_row(conn, team_id, chunk, report)
            logger.info(
                "disposition_pull: chunk %d/%d done (%d/%d rows)",
                i, len(chunks),
                min(i * CHUNK_SIZE, len(records)), len(records),
            )
    return report


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


async def pull_once(*, is_today: bool = True,
                    days_start: int = 1, days_end: int = 20) -> dict:
    """One fetch+fill cycle across the configured call centers."""
    team_id = pull_team()
    totals = {"rows_in_export": 0, "with_disposition": 0,
              "calls_written": 0, "evals_filled": 0, "row_failures": 0}
    for target in pull_targets():
        text = await fetch_export(
            target, is_today=is_today,
            days_start=days_start, days_end=days_end,
        )
        report = await fill_records(team_id, parse_export_csv(text))
        for key in totals:
            totals[key] += report[key]
    return totals


async def run_periodic_pull(interval_minutes: float) -> None:
    """Every `interval_minutes`: pull today's dispositions. Failures log
    and wait for the next tick — the loop itself never dies."""
    logger.info(
        "disposition_pull: periodic loop up (every %.0f min, targets=%s)",
        interval_minutes, ",".join(pull_targets()),
    )
    while True:
        try:
            report = await pull_once(is_today=True)
            logger.info(
                "disposition_pull: %(rows_in_export)d rows, "
                "%(with_disposition)d dispositioned, "
                "%(calls_written)d calls written, %(evals_filled)d evals "
                "filled, %(row_failures)d failures", report,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("disposition_pull: cycle failed — next tick continues")
        await asyncio.sleep(interval_minutes * 60)


def periodic_interval_minutes() -> Optional[float]:
    """Parsed CC_STATS_PULL_INTERVAL_MIN, or None when the loop is off
    (unset, empty, non-numeric, or <= 0)."""
    raw = os.environ.get("CC_STATS_PULL_INTERVAL_MIN", "").strip()
    if not raw:
        return None
    try:
        interval = float(raw)
    except ValueError:
        logger.error(
            "disposition_pull: CC_STATS_PULL_INTERVAL_MIN=%r is not numeric "
            "— loop disabled", raw,
        )
        return None
    return interval if interval > 0 else None

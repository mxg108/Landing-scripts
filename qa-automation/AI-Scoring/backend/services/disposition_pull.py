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
            await asyncio.sleep(POLL_INTERVAL_S)

        download = await client.get(url, follow_redirects=True)
        download.raise_for_status()
        return download.text


# ---------------------------------------------------------------------------
# DB fill — UPSERT, structurally idempotent
# ---------------------------------------------------------------------------

# The conditional DO UPDATE only fires when the existing row has no
# disposition yet (webhook-era stamps and earlier pulls win); the WHERE
# also stops rewriting identical NULL→NULL rows every half hour.
_CALLS_UPSERT = """
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

_EVALS_FILL = """
UPDATE qa.evaluations
SET dialpad_disposition_category = $3,
    dialpad_disposition = $4
WHERE team_id = $1 AND dialpad_disposition_category IS NULL
  AND (dialpad_call_id = $2
       OR dialpad_entry_point_call_id = $2
       OR dialpad_master_call_id = $2)
"""


async def fill_records(
    team_id: str, records: list[StatsRecord], *, dry_run: bool = False
) -> dict:
    """Fill both tables from parsed export records. Row failures are
    caught, logged, and counted — a bad row never kills the batch."""
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

    async with pool.acquire() as conn:
        for record in records:
            try:
                if dry_run:
                    hit = await conn.fetchval(
                        "SELECT 1 FROM command_center.calls "
                        "WHERE team_id = $1 AND dialpad_call_id = $2 "
                        "  AND disposition_source IS NOT NULL",
                        team_id, record.call_id,
                    )
                    if hit is None:
                        report["calls_written"] += 1
                    continue
                result = await conn.execute(
                    _CALLS_UPSERT,
                    team_id, record.call_id,
                    record.disposition_category, record.disposition,
                    record.direction, record.external_number,
                    record.internal_number, record.agent_name,
                    record.started_at, record.connected_at, record.ended_at,
                )
                report["calls_written"] += int(result.split()[-1])
                if record.disposition_category:
                    ev = await conn.execute(
                        _EVALS_FILL,
                        team_id, record.call_id,
                        record.disposition_category, record.disposition,
                    )
                    report["evals_filled"] += int(ev.split()[-1])
            except Exception:
                report["row_failures"] += 1
                logger.exception(
                    "disposition_pull: row failed call_id=%s", record.call_id
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

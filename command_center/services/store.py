"""Webhook-event persistence: append-only log + fold write-through.

The transaction per event (§4.1.2 invariant — live and replay are the
same function):

  1. INSERT INTO webhook_events, deduped by the 005 partial UNIQUE
     indexes (an accidental redelivery is a no-op — the fold is skipped
     because it already ran when the event first landed).
  2. SELECT the calls row FOR UPDATE, fold (pure — services/fold.py),
     upsert calls, insert the materialized hold cycle if one closed.
  3. Stamp webhook_events.processed_at.

Pool management mirrors backend/services/eval_store.py: lazy on first
write, DATABASE_URL from env, closed by the FastAPI lifespan teardown.
No DATABASE_URL → events are acked but not stored (logged loudly);
Dialpad retries are not the place to queue an outage.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Optional

from command_center.services import fold

logger = logging.getLogger(__name__)

_pool = None
_pool_lock: Optional[asyncio.Lock] = None

# Allow-list for dynamic upsert SQL — every calls column the fold may set.
_CALL_COLUMNS = frozenset({
    "team_id", "dialpad_call_id", "dialpad_master_call_id",
    "dialpad_entry_point_call_id", "dialpad_agent_id", "agent_name",
    "target_name", "target_type", "caller_name", "caller_email",
    "direction", "external_number", "internal_number", "group_id",
    "is_transferred", "was_recorded", "mos_score",
    "started_at", "rang_at", "connected_at", "ended_at",
    "total_duration_ms", "last_state", "last_state_at",
    "total_hold_seconds",
    "disposition_category", "disposition", "disposition_source", "ai_csat",
})


async def _get_pool():
    global _pool, _pool_lock
    if _pool is not None:
        return _pool
    import os
    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn:
        return None
    if _pool_lock is None:
        _pool_lock = asyncio.Lock()
    async with _pool_lock:
        if _pool is None:
            import asyncpg
            _pool = await asyncpg.create_pool(dsn, min_size=0, max_size=4, timeout=5)
    return _pool


async def close_pool() -> None:
    """Lifespan teardown."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def reset_pool_for_tests() -> None:
    """Drop the cached pool so tests can repoint DATABASE_URL."""
    global _pool, _pool_lock
    _pool = None
    _pool_lock = None


async def ingest_event(team_id: str, payload: dict) -> str:
    """Append + fold one decoded webhook payload.

    Returns 'ingested', 'duplicate' (dedupe hit — already folded),
    'appended' (agent-status / non-call event: logged, no fold), or
    'no_db' (DATABASE_URL unset).
    """
    ev = fold.normalize_event(payload)
    pool = await _get_pool()
    if pool is None:
        logger.error(
            "cc.store: DATABASE_URL unset — %s event for call %s acked but NOT stored",
            ev.event_kind, ev.dialpad_call_id,
        )
        return "no_db"

    async with pool.acquire() as conn:
        async with conn.transaction():
            event_id = await _append_event(conn, team_id, ev)
            if event_id is None:
                return "duplicate"
            if ev.event_kind != "call":
                return "appended"
            await _fold_into_calls(conn, team_id, ev)
            await conn.execute(
                "UPDATE command_center.webhook_events SET processed_at = NOW() "
                "WHERE id = $1",
                event_id,
            )
    return "ingested"


async def _append_event(conn, team_id: str, ev: fold.NormalizedEvent) -> Optional[int]:
    """Append-only write; None when the partial UNIQUE dedupe fired."""
    if ev.event_kind == "call":
        conflict = (
            "ON CONFLICT (dialpad_call_id, state, event_timestamp) "
            "WHERE event_kind = 'call' DO NOTHING"
        )
    else:
        conflict = (
            "ON CONFLICT (dialpad_agent_id, state, event_timestamp) "
            "WHERE event_kind = 'agent_status' DO NOTHING"
        )
    return await conn.fetchval(
        f"""
        INSERT INTO command_center.webhook_events
            (team_id, event_kind, dialpad_call_id, dialpad_master_call_id,
             dialpad_agent_id, state, event_timestamp, raw_payload)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)
        {conflict}
        RETURNING id
        """,
        team_id, ev.event_kind, ev.dialpad_call_id,
        ev.dialpad_master_call_id, ev.dialpad_agent_id,
        ev.state, ev.event_timestamp, json.dumps(ev.payload),
    )


async def _fold_into_calls(conn, team_id: str, ev: fold.NormalizedEvent) -> None:
    prior = await conn.fetchrow(
        "SELECT id, last_state, last_state_at, total_hold_seconds "
        "FROM command_center.calls "
        "WHERE team_id = $1 AND dialpad_call_id = $2 "
        "FOR UPDATE",
        team_id, ev.dialpad_call_id,
    )
    result = fold.fold_call_event(dict(prior) if prior else None, ev, team_id)

    unknown = set(result.call_columns) - _CALL_COLUMNS
    if unknown:
        raise ValueError(f"fold produced unknown calls columns: {sorted(unknown)}")

    if prior is None:
        cols = dict(result.call_columns)
        cols["seen_via"] = "webhook"
        names = ", ".join(cols)
        placeholders = ", ".join(f"${i + 1}" for i in range(len(cols)))
        call_id = await conn.fetchval(
            f"INSERT INTO command_center.calls ({names}) "
            f"VALUES ({placeholders}) RETURNING id",
            *cols.values(),
        )
    else:
        call_id = prior["id"]
        updates = {
            k: v for k, v in result.call_columns.items()
            if k not in ("team_id", "dialpad_call_id")
        }
        updates["last_updated_at"] = datetime.now(timezone.utc)
        assignments = ", ".join(
            f"{name} = ${i + 2}" for i, name in enumerate(updates)
        )
        await conn.execute(
            f"UPDATE command_center.calls SET {assignments} WHERE id = $1",
            call_id, *updates.values(),
        )

    if result.hold_interval is not None:
        hi = result.hold_interval
        await conn.execute(
            """
            INSERT INTO command_center.hold_intervals
                (team_id, dialpad_call_id, call_id, started_at, ended_at,
                 seconds, ended_by)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            hi["team_id"], hi["dialpad_call_id"], call_id,
            hi["started_at"], hi["ended_at"], hi["seconds"], hi["ended_by"],
        )

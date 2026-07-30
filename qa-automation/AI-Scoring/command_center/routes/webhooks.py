"""POST /api/webhooks/dialpad — JWT-body-decoded webhook receiver.

Mounted into the AI-Scoring FastAPI app WITHOUT the API-key auth
dependency: Dialpad is the caller, and the JWT signature (subscription
secret, `DIALPAD_WEBHOOK_SECRET` in env) IS the authentication.

Response discipline: 401 only for signature failures (a retry cannot
fix a tampered/foreign payload... but Dialpad drops subscriptions that
persistently error, so misconfiguration must be loud in logs, not in
status codes). Everything verified is acked 200 — including events we
choose not to store — because Dialpad's retry queue is not our backlog.
"""

from __future__ import annotations

import json
import logging
import os
import time
from functools import lru_cache
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from command_center.services import fold, store

# App-side seam ONLY here in the route adapter: fold/store stay
# framework-free (Sandy carries them verbatim); the event bus and the
# qa.agents roster lookup are the hosting app's plumbing and are
# re-pointed at re-platform time along with the subscription URL.
from backend.services.event_bus import get_event_bus
from backend.services.data_normalization import strip_accents

logger = logging.getLogger(__name__)

# Roster cache TTL for agent_status → team scoping. Roster churn is
# human-paced (import_agents runs / departures) — 5 min staleness is
# invisible next to a toast's 4s TTL.
_ROSTER_TTL_S = 300.0
_roster_cache: Optional[tuple[float, dict[str, set], dict[str, set]]] = None

router = APIRouter()

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "command_center.json"


@lru_cache(maxsize=1)
def _team_by_target_id() -> dict[str, str]:
    """Flatten config: Dialpad call-center/office id → team_id."""
    config = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    mapping: dict[str, str] = {}
    for team_id, team_cfg in config.get("teams", {}).items():
        dialpad = team_cfg.get("dialpad", {})
        for cc_id in dialpad.get("target_call_center_ids", []):
            mapping[str(cc_id)] = team_id
        if dialpad.get("office_id"):
            mapping[str(dialpad["office_id"])] = team_id
    return mapping


def resolve_team(payload: dict) -> Optional[str]:
    """Map an event to a team via the configured Dialpad ids.

    Checks the target/group/call-center ids the payload may carry. The
    subscription is created per call center (§4), so in practice every
    event maps; unmatched events are logged and acked without storage.
    """
    mapping = _team_by_target_id()
    candidates = []
    target = payload.get("target") or {}
    if isinstance(target, dict) and target.get("id") not in (None, ""):
        candidates.append(str(target["id"]))
    for key in ("call_center_id", "group_id", "office_id"):
        if payload.get(key) not in (None, ""):
            candidates.append(str(payload[key]))
    for candidate in candidates:
        if candidate in mapping:
            return mapping[candidate]
    # Single-team fallback: with exactly one configured team, an event
    # arriving on our subscription belongs to it even when the payload
    # carries none of the mapped ids (target may be the individual user).
    teams = set(mapping.values())
    if len(teams) == 1:
        return next(iter(teams))
    return None


@router.post("/api/webhooks/dialpad")
async def dialpad_webhook(request: Request) -> dict:
    secret = os.environ.get("DIALPAD_WEBHOOK_SECRET", "")
    if not secret:
        logger.error("cc.webhooks: DIALPAD_WEBHOOK_SECRET unset — rejecting event")
        raise HTTPException(status_code=503, detail="webhook secret not configured")

    body = await request.body()
    try:
        payload = fold.verify_and_decode(body, secret)
    except fold.SignatureError as e:
        logger.warning("cc.webhooks: signature verification failed: %s", e)
        raise HTTPException(status_code=401, detail="invalid signature")

    team_id = resolve_team(payload)
    if team_id is None:
        logger.warning(
            "cc.webhooks: no team match for event (state=%s call_id=%s target=%s) — acked, NOT stored",
            payload.get("state"), payload.get("call_id"), payload.get("target"),
        )
        return {"status": "unmatched"}

    try:
        status = await store.ingest_event(team_id, payload)
    except Exception:
        # Response discipline (module docstring): a storage fault must be
        # loud in logs, not in status codes — Dialpad's retries cannot fix
        # a shape/DB fault, and persistent 5xx gets the subscription
        # auto-disabled (nearly happened 2026-07-29: blank agent-status
        # `state` violated the 005 CHECK and 500'd every delivery).
        logger.exception(
            "cc.webhooks: ingest failed — event acked 200, NOT stored "
            "(state=%s call_id=%s)",
            payload.get("state"), payload.get("call_id"),
        )
        status = "error"

    # Live pulse: surface agent-status changes on the team dashboard via
    # the existing SSE stream. Skip dedupe hits — a Dialpad redelivery
    # must not re-toast. The pulse is independent of persistence, so an
    # 'error' ingest still toasts. Publish failures never break the ack.
    if status != "duplicate":
        ev = fold.normalize_event(payload)
        if ev.event_kind == "agent_status":
            await _publish_agent_status(team_id, ev)
    return {"status": status}


async def _roster_maps() -> Optional[tuple[dict[str, set], dict[str, set]]]:
    """(dialpad_agent_id → {team_id}, folded name → {team_id}) over the
    ACTIVE qa.agents roster, TTL-cached. None when no DB is configured
    or the read fails — callers fall back to the resolve_team() team so
    local dev (no DATABASE_URL) still toasts."""
    global _roster_cache
    now = time.monotonic()
    if _roster_cache is not None and now - _roster_cache[0] < _ROSTER_TTL_S:
        return _roster_cache[1], _roster_cache[2]
    pool = await store.get_pool()
    if pool is None:
        return None
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT team_id, name, canonical_name, dialpad_agent_id "
                "FROM qa.agents WHERE active"
            )
    except Exception:
        logger.exception("cc.webhooks: roster read failed — agent_status "
                         "falls back to single-team publish")
        return None
    by_id: dict[str, set] = {}
    by_name: dict[str, set] = {}
    for r in rows:
        if r["dialpad_agent_id"]:
            by_id.setdefault(str(r["dialpad_agent_id"]), set()).add(r["team_id"])
        for n in (r["name"], r["canonical_name"]):
            if n:
                key = strip_accents(str(n)).lower().strip()
                by_name.setdefault(key, set()).add(r["team_id"])
    _roster_cache = (now, by_id, by_name)
    return by_id, by_name


async def _publish_agent_status(fallback_team_id: str, ev: fold.NormalizedEvent) -> None:
    """SSE 'agent_status' → the toast rail of the agent's OWN team(s).

    The agent-status subscription covers agents org-wide (Verifications
    etc. arrived on the MS dashboard via the single-team fallback), so
    the publish is scoped by qa.agents roster match: dialpad_agent_id
    first (§3.11 eager-resolve), accent-folded name second. No roster
    match → no toast (the event is still stored). Roster unavailable
    (no DB / read failure) → legacy single-team publish.

    Payload-shape defensiveness mirrors DispositionDesign §9: the agent
    display name is target.name when the target is the user, with
    payload-level name fields as fallbacks; blanks render placeholders
    client-side rather than dropping the event.
    """
    payload = ev.payload
    target = payload.get("target") or {}
    name = ""
    if isinstance(target, dict) and target.get("type") == "user":
        name = str(target.get("name") or "")
    if not name:
        name = str(payload.get("name") or payload.get("display_name") or "")
    status_label = str(ev.state or payload.get("agent_state") or "")

    maps = await _roster_maps()
    if maps is None:
        teams = {fallback_team_id}
    else:
        by_id, by_name = maps
        teams = set()
        if ev.dialpad_agent_id:
            teams |= by_id.get(ev.dialpad_agent_id, set())
        if not teams and name:
            teams |= by_name.get(strip_accents(name).lower().strip(), set())

    logger.info(
        "cc.webhooks: agent_status agent=%s state=%s teams=%s keys=%s",
        ev.dialpad_agent_id, status_label or "?",
        sorted(teams) or "-", sorted(payload.keys()),
    )
    if not teams:
        return
    try:
        bus = get_event_bus()
        for team in sorted(teams):
            await bus.publish(team, "agent_status", {
                "agent_id": ev.dialpad_agent_id,
                "agent": name,
                "status": status_label,
                "at": ev.event_timestamp.isoformat(),
            })
    except Exception:
        logger.exception("cc.webhooks: agent_status publish failed (non-fatal)")

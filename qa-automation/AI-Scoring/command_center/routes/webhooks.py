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
from functools import lru_cache
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from command_center.services import fold, store

# App-side seam ONLY here in the route adapter: fold/store stay
# framework-free (Sandy carries them verbatim); the event bus is the
# hosting app's live-dashboard plumbing and is re-pointed at re-platform
# time along with the subscription URL.
from backend.services.event_bus import get_event_bus

logger = logging.getLogger(__name__)

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

    status = await store.ingest_event(team_id, payload)

    # Live pulse: surface agent-status changes on the team dashboard via
    # the existing SSE stream. Skip dedupe hits — a Dialpad redelivery
    # must not re-toast. Publish failures never break the ack (Dialpad
    # drops subscriptions that persistently error).
    if status != "duplicate":
        ev = fold.normalize_event(payload)
        if ev.event_kind == "agent_status":
            await _publish_agent_status(team_id, ev)
    return {"status": status}


async def _publish_agent_status(team_id: str, ev: fold.NormalizedEvent) -> None:
    """SSE 'agent_status' → the team dashboard's toast rail.

    Payload-shape defensiveness mirrors DispositionDesign §9: the agent
    display name is target.name when the target is the user, with
    payload-level name fields as fallbacks; the status label is the
    event's state. Both may be blank on shapes we haven't observed —
    the client renders placeholders rather than dropping the event.
    """
    payload = ev.payload
    target = payload.get("target") or {}
    name = ""
    if isinstance(target, dict) and target.get("type") == "user":
        name = str(target.get("name") or "")
    if not name:
        name = str(payload.get("name") or payload.get("display_name") or "")
    status_label = str(ev.state or payload.get("agent_state") or "")

    logger.info(
        "cc.webhooks: agent_status agent=%s state=%s keys=%s",
        ev.dialpad_agent_id, status_label or "?", sorted(payload.keys()),
    )
    try:
        await get_event_bus().publish(team_id, "agent_status", {
            "agent_id": ev.dialpad_agent_id,
            "agent": name,
            "status": status_label,
            "at": ev.event_timestamp.isoformat(),
        })
    except Exception:
        logger.exception("cc.webhooks: agent_status publish failed (non-fatal)")

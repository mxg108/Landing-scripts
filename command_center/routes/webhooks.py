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
    return {"status": status}

"""Dialpad user lookup routes."""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from backend.middleware.auth import KeyIdentity, require_api_key, team_id_from_path
from backend.services.dialpad_client import (
    get_recording_share_link,
    get_user_by_email,
    list_calls_for_user,
)
from backend.services.history_service import resolve_team_for_agent

router = APIRouter(prefix="/lookup", tags=["lookup"])


class ScoringPermission(BaseModel):
    """Response shape for /lookup/scoring-permission."""
    agent_email: str
    resolved_team: Optional[str]
    can_score: bool
    needs_team_pick: bool


@router.get("")
async def lookup_user(email: str = Query(..., description="Dialpad user email")):
    """Look up a Dialpad user by email. Returns parsed user profile."""
    user = await get_user_by_email(email)
    if not user:
        raise HTTPException(status_code=404, detail=f"No Dialpad user found for '{email}'")
    return user


@router.get("/calls")
async def lookup_calls(
    user_id: str = Query(..., description="Dialpad user ID"),
    date_start: Optional[str] = Query(None, description="Start date ISO (YYYY-MM-DDTHH:MM)"),
    date_end: Optional[str] = Query(None, description="End date ISO (YYYY-MM-DDTHH:MM)"),
    cursor: Optional[str] = Query(None, description="Pagination cursor from previous response"),
    limit: int = Query(10, ge=1, le=100),
):
    """Fetch calls for a Dialpad user. Dates are local ISO strings, converted to epoch ms."""
    started_after_ms = None
    started_before_ms = None

    if date_start:
        try:
            dt = datetime.fromisoformat(date_start)
            started_after_ms = int(dt.timestamp() * 1000)
        except ValueError:
            raise HTTPException(400, "date_start must be ISO format (YYYY-MM-DDTHH:MM)")

    if date_end:
        try:
            dt = datetime.fromisoformat(date_end)
            started_before_ms = int(dt.timestamp() * 1000)
        except ValueError:
            raise HTTPException(400, "date_end must be ISO format (YYYY-MM-DDTHH:MM)")

    calls, next_cursor = await list_calls_for_user(
        user_id, started_after_ms, started_before_ms, limit, cursor
    )
    return {
        "user_id": user_id,
        "call_count": len(calls),
        "calls": calls,
        "cursor": next_cursor,
    }


@router.post("/recording-link")
async def generate_recording_link(
    recording_id: str = Query(..., description="Dialpad recording ID"),
    recording_type: str = Query("admincallrecording", description="Recording type"),
):
    """Generate a shareable recording link via Dialpad API."""
    link = await get_recording_share_link(recording_id, recording_type)
    if not link:
        raise HTTPException(404, "Could not generate recording link")
    return {"link": link}


@router.get("/scoring-permission", response_model=ScoringPermission)
async def scoring_permission(
    request: Request,
    email: str = Query(..., description="Agent email to probe"),
    identity: KeyIdentity = Depends(require_api_key),
):
    """Lightweight permission probe used by the frontend Score Call button.

    Returns enough info for the client to render each /lookup row's
    button state without re-implementing the Mails membership rule:

      * ``resolved_team`` — first team whose Mails roster contains
        ``email``, or null if unrostered anywhere.
      * ``can_score`` — whether the calling key may post /score for this
        email + the requested team_id (path).
        - Team key: must match the resolved team.
        - Privileged key: always true; the operator confirms target via
          the team-pick dialog when unrostered.
      * ``needs_team_pick`` — true only for privileged callers on an
        unrostered agent; signal for the frontend to open the modal.
    """
    team_id = team_id_from_path(request)
    resolved_team = await resolve_team_for_agent(email)

    if identity.role == "privileged":
        can_score = True
        needs_team_pick = resolved_team is None
    else:
        can_score = resolved_team == identity.team_id == team_id
        needs_team_pick = False

    return ScoringPermission(
        agent_email=email,
        resolved_team=resolved_team,
        can_score=can_score,
        needs_team_pick=needs_team_pick,
    )

"""Dialpad user lookup routes."""

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from backend.services.dialpad_client import get_user_by_email, list_calls_for_user, get_recording_share_link

router = APIRouter(prefix="/api", tags=["lookup"])


@router.get("/lookup")
async def lookup_user(email: str = Query(..., description="Dialpad user email")):
    """Look up a Dialpad user by email. Returns parsed user profile."""
    user = await get_user_by_email(email)
    if not user:
        raise HTTPException(status_code=404, detail=f"No Dialpad user found for '{email}'")
    return user


@router.get("/lookup/calls")
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


@router.post("/lookup/recording-link")
async def generate_recording_link(
    recording_id: str = Query(..., description="Dialpad recording ID"),
    recording_type: str = Query("admincallrecording", description="Recording type"),
):
    """Generate a shareable recording link via Dialpad API."""
    link = await get_recording_share_link(recording_id, recording_type)
    if not link:
        raise HTTPException(404, "Could not generate recording link")
    return {"link": link}

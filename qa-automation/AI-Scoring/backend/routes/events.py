"""Server-Sent Events endpoint for live dashboard updates.

Single endpoint: GET /events/team/{team_id}?api_key=...

Auth is the same `require_api_key` used everywhere else; the
query-param fallback (Phase 0 of LiveDashboard.md) exists for
`EventSource`, which cannot set Authorization headers.

Stream protocol:
  - On connect: yields a SSE comment (": connected") to flush headers
    immediately past any buffering proxy.
  - On publish: yields `event: <name>\\ndata: <json>\\n\\n` per event.
  - Every 15s of idleness: yields a SSE comment heartbeat to keep
    Railway's proxy from closing the connection (default idle ~60s).
  - On client disconnect: the consumer iterator stops; the `finally`
    block unsubscribes from the bus.

Caller filter-state isn't tracked here. Every subscriber on a given
team gets every event; client-side handlers decide what to do.
"""

from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from backend.middleware.auth import team_id_from_path
from backend.services.event_bus import get_event_bus


router = APIRouter(tags=["events"])

# Heartbeat cadence. Tuned shorter than Railway's default idle close
# (~60s) so a quiet team's dashboard stays connected.
_HEARTBEAT_SECONDS = 15.0


async def _stream(team_id: str) -> AsyncIterator[bytes]:
    bus = get_event_bus()
    q = bus.subscribe(team_id)
    # SSE comment lines (start with `:`) are ignored by EventSource but
    # flush headers and keep intermediaries warm.
    yield b": connected\n\n"
    try:
        while True:
            try:
                evt = await asyncio.wait_for(q.get(), timeout=_HEARTBEAT_SECONDS)
            except asyncio.TimeoutError:
                yield b": heartbeat\n\n"
                continue
            payload = json.dumps(evt["data"], default=str)
            yield f"event: {evt['event']}\ndata: {payload}\n\n".encode("utf-8")
    finally:
        bus.unsubscribe(team_id, q)


@router.get("/events/stream")
async def stream_team_events(request: Request) -> StreamingResponse:
    # team_id comes from the router-mount prefix (/api/{team_id}/...).
    # Auth is enforced by the router-level TEAM_AUTH_DEPENDENCY mount in
    # main.py (and mirrored in the test fixture). No inner Depends here.
    team_id = team_id_from_path(request)
    return StreamingResponse(
        _stream(team_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Disable buffering on nginx-style proxies if any sit in front.
            "X-Accel-Buffering": "no",
        },
    )

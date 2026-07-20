"""
Dialpad API client.
Handles: user lookup, call listing by agent + timeframe, transcript fetching,
recording download.
"""

import asyncio
import os
from datetime import datetime, timedelta, timezone
from typing import Optional
import httpx

BASE_URL = "https://dialpad.com/api/v2"
CALL_DURATION_FLAG_MS = 25 * 60 * 1000  # 25 minutes — flag for manager review

# Cap concurrent Dialpad requests. Multi-upload flows fan out background tasks
# that all hit /transcripts and /call/{id}; without this, bursts get 429'd and
# silently return empty metadata for every call after the first.
_SEMAPHORE = asyncio.Semaphore(5)


class DialpadRateLimited(Exception):
    """Raised when Dialpad returns 429 — caller may choose to retry."""


class NoRecordingAvailable(Exception):
    """Raised when a call has no associated recording to download."""

def _headers() -> Optional[dict]:
    token = os.getenv("DIALPAD_API_KEY")
    if not token:
        print("No API token")
        return None
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# User / agent lookup
# ---------------------------------------------------------------------------

async def get_user_by_email(email: str) -> Optional[dict]:
    """
    Look up a Dialpad user by email via GET /users?email=.
    Returns a parsed dict with the fields the frontend needs, or None.
    """
    hdrs = _headers()
    if not hdrs:
        return None

    try:
        async with _SEMAPHORE, httpx.AsyncClient() as client:
            resp = await client.get(
                f"{BASE_URL}/users",
                headers=hdrs,
                params={"email": email},
                timeout=15,
            )
            resp.raise_for_status()
            items = resp.json().get("items", [])
    except httpx.HTTPStatusError as e:
        print(f"[dialpad_client] user lookup by email failed ({e.response.status_code}): {e}")
        return None
    except httpx.RequestError as e:
        print(f"[dialpad_client] user lookup request error: {e}")
        return None

    if not items:
        return None

    u = items[0]
    return {
        "id": str(u.get("id", "")),
        "display_name": u.get("display_name", ""),
        "first_name": u.get("first_name", ""),
        "last_name": u.get("last_name", ""),
        "emails": u.get("emails", []),
        "extension": u.get("extension", ""),
        "phone_numbers": u.get("phone_numbers", []),
        "job_title": u.get("job_title", ""),
        "state": u.get("state", ""),
        "license": u.get("license", ""),
        "is_admin": u.get("is_admin", False),
        "is_online": u.get("is_online", False),
        "is_available": u.get("is_available", False),
        "is_on_duty": u.get("is_on_duty", False),
        "on_duty_status": u.get("on_duty_status", ""),
        "timezone": u.get("timezone", ""),
        "office_id": str(u.get("office_id", "")),
        "date_added": u.get("date_added", ""),
        "duty_status_started": u.get("duty_status_started", ""),
        "groups": [
            {
                "group_id": str(g.get("group_id", "")),
                "group_type": g.get("group_type", ""),
                "role": g.get("role", ""),
            }
            for g in u.get("group_details", [])
        ],
    }


async def get_user_id_by_name(agent_name: str) -> Optional[str]:
    """
    Resolve a human agent name to a Dialpad user_id.
    Matches case-insensitively on display_name or first+last name.
    Returns None if no match found.
    """
    async with _SEMAPHORE, httpx.AsyncClient() as client:
        response = await client.get(
            f"{BASE_URL}/users",
            headers=_headers(),
            params={"state": "active"},
            timeout=15,
        )
        response.raise_for_status()
        users = response.json().get("items", [])

    name_lower = agent_name.strip().lower()
    for user in users:
        display = user.get("display_name", "").lower()
        first = user.get("first_name", "").lower()
        last = user.get("last_name", "").lower()
        full = f"{first} {last}".strip()
        if name_lower in (display, full, first, last):
            return str(user["id"])
    return None


async def list_calls_for_user(
    user_id: str,
    started_after_ms: Optional[int] = None,
    started_before_ms: Optional[int] = None,
    limit: int = 10,
    cursor: Optional[str] = None,
) -> tuple[list[dict], Optional[str]]:
    """
    Fetch recent calls for a user via GET /call?target_id=&target_type=user.
    Timestamps are epoch milliseconds (UTC). Returns (parsed call dicts, next_cursor).
    """
    hdrs = _headers()
    if not hdrs:
        return [], None

    params: dict = {
        "target_id": user_id,
        "target_type": "user",
        "limit": limit,
    }
    if started_after_ms is not None:
        params["started_after"] = started_after_ms
    if started_before_ms is not None:
        params["started_before"] = started_before_ms
    if cursor:
        params["cursor"] = cursor

    try:
        async with _SEMAPHORE, httpx.AsyncClient() as client:
            resp = await client.get(
                f"{BASE_URL}/call",
                headers=hdrs,
                params=params,
                timeout=20,
            )
            resp.raise_for_status()
            data = resp.json()
            items = data.get("items", [])
            next_cursor = data.get("cursor") or None
    except httpx.HTTPStatusError as e:
        print(f"[dialpad_client] list_calls_for_user failed ({e.response.status_code}): {e}")
        return [], None
    except httpx.RequestError as e:
        print(f"[dialpad_client] list_calls_for_user request error: {e}")
        return [], None

    calls = []
    for c in items:
        duration = c.get("total_duration", 0) or c.get("duration", 0) or 0
        calls.append({
            "call_id": str(c.get("call_id", "")),
            "date_started": _epoch_to_iso(c.get("date_started")),
            "date_connected": _epoch_to_iso(c.get("date_connected")),
            "date_ended": _epoch_to_iso(c.get("date_ended")),
            "duration": duration,
            "direction": c.get("direction", ""),
            "was_recorded": bool(c.get("recording_details")),
            "recording_id": str(c.get("recording_details", [{}])[0].get("id", "")) if c.get("recording_details") else "",
            "recording_url": c.get("recording_details", [{}])[0].get("url", "") if c.get("recording_details") else "",
            "recording_duration": int(c.get("recording_details", [{}])[0].get("duration", 0)) if c.get("recording_details") else 0,
            "recording_type": c.get("recording_details", [{}])[0].get("recording_type", "") if c.get("recording_details") else "",
            "is_transferred": c.get("is_transferred", False),
            "external_number": c.get("external_number", ""),
            "internal_number": c.get("internal_number", ""),
            "contact_name": (c.get("contact") or {}).get("name", ""),
            "contact_phone": (c.get("contact") or {}).get("phone", ""),
            "mos_score": c.get("mos_score"),
            "entry_point_call_id": str(c.get("entry_point_call_id", "")),
            "_flagged_long_call": duration > CALL_DURATION_FLAG_MS,
        })
    return calls, next_cursor


def _epoch_to_iso(val) -> str:
    """Convert an epoch-ms timestamp (int or numeric string) to ISO string."""
    if val is None:
        return ""
    try:
        ts = int(val) / 1000
        return datetime.fromtimestamp(ts).isoformat()
    except (ValueError, TypeError, OSError):
        return str(val)


def _epoch_ms_to_utc_datetime(val) -> Optional[datetime]:
    """Convert a Dialpad epoch-ms timestamp to a UTC-aware datetime.

    Used by the call-time initiative (PR-1 of
    references/CallTimeOnAnalystHistory.md): `get_call_details` returns
    `date_connected` / `date_ended` as numeric epoch-ms strings (or
    empty when Dialpad didn't supply one). The writer needs a real
    `datetime` for formatting + plumbing through `ScorecardWithMeta`.

    Returns None on any failure so the writer can detect "we don't know"
    and leave the cell blank, rather than guessing.
    """
    if val is None or val == "":
        return None
    try:
        secs = int(val) / 1000
        return datetime.fromtimestamp(secs, tz=timezone.utc)
    except (ValueError, TypeError, OSError):
        return None


def compute_call_duration(
    call_started_at_utc: Optional[datetime],
    call_ended_at_utc: Optional[datetime],
) -> Optional[timedelta]:
    """Difference between Dialpad's `date_ended` and `date_connected`.

    Plumbed in the call-time initiative (PR-1) but NOT consumed by any
    production code yet. The future "call duration as an analytics
    dimension" project will pick this up without having to re-touch
    scoring_service / ScorecardWithMeta. Returns None when either input
    is missing.
    """
    if call_started_at_utc is None or call_ended_at_utc is None:
        return None
    return call_ended_at_utc - call_started_at_utc


# ---------------------------------------------------------------------------
# Call listing (stats — used by scoring pipeline)
# ---------------------------------------------------------------------------

async def get_calls_for_agent(
    user_id: str,
    date_start: datetime,
    date_end: datetime,
) -> list[dict]:
    """
    Fetch calls for a given agent within a date range.
    Returns list of call objects. Calls over 25 min are flagged but included.
    """
    # Dialpad expects epoch milliseconds
    started_after = int(date_start.timestamp() * 1000)
    started_before = int(date_end.timestamp() * 1000)

    async with _SEMAPHORE, httpx.AsyncClient() as client:
        response = await client.get(
            f"{BASE_URL}/stats/calls",
            headers=_headers(),
            params={
                "started_after": started_after,
                "started_before": started_before,
                "target_type": "user",
                "target_id": user_id,
                "limit": 100,
            },
            timeout=20,
        )
        response.raise_for_status()
        data = response.json()

    calls = data.get("items", [])

    # Annotate each call with a long-call flag
    for call in calls:
        duration = call.get("duration", 0) or 0
        call["_flagged_long_call"] = duration > CALL_DURATION_FLAG_MS

    return calls


def build_dialpad_link(call_id: str, entry_point_call_id: str = "") -> str:
    """Construct the Dialpad web link for a call (used by scoring pipeline).

    Dialpad's recording-page URL is keyed by ``entry_point_call_id`` — the
    id Dialpad assigns to the call's *entry point* (queue, ring group, etc.)
    — NOT the per-leg ``call_id`` returned by ``/api/v2/call/{id}``. For
    inbound calls that go through a queue the two differ; for direct calls
    they may match. The recording page only exists at the entry-point URL,
    so analysts clicking through to "the call in Dialpad" need that one.

    ``entry_point_call_id`` is preferred when supplied (callers should pass
    it from ``get_call_details``'s response); falls back to ``call_id`` so
    code paths that don't have the entry-point id still produce a working
    link for direct calls and for callers that haven't been updated.
    """
    target = (entry_point_call_id or "").strip() or call_id
    return f"https://dialpad.com/callhistory/callreview/{target}"


async def get_recording_share_link(
    recording_id: str,
    recording_type: str = "admincallrecording",
) -> Optional[str]:
    """
    Generate a shareable recording link via POST /recordingsharelink.
    Returns the share URL, or None on failure.
    """
    hdrs = _headers()
    if not hdrs or not recording_id:
        return None

    hdrs["Content-Type"] = "application/json"
    try:
        async with _SEMAPHORE, httpx.AsyncClient() as client:
            resp = await client.post(
                f"{BASE_URL}/recordingsharelink",
                headers=hdrs,
                json={
                    "privacy": "company",
                    "recording_type": recording_type,
                    "recording_id": recording_id,
                },
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("access_link") or None
    except httpx.HTTPStatusError as e:
        print(f"[dialpad_client] recording share link failed ({e.response.status_code}): {e}")
        return None
    except httpx.RequestError as e:
        print(f"[dialpad_client] recording share link request error: {e}")
        return None


async def get_call_details(call_id: str) -> dict:
    """
    Fetch call metadata from Dialpad GET /api/v2/call/{call_id}.

    Returns the full payload flattened into stable top-level keys, plus
    `raw` carrying the unmodified API response. All existing keys are
    preserved for back-compat; new fields are additive so downstream
    Postgres writers (qa.evaluations, command_center.calls) can persist
    every metadata field without a second API round-trip.

    Non-fatal — returns empty defaults on failure.
    """
    empty = {
        # --- caller (existing) ---
        "caller_name": "",
        "caller_phone": "",
        "caller_email": "",
        # --- caller (new: full contact dict) ---
        "contact_id": "",
        "contact_type": "",
        # --- call routing (existing) ---
        "direction": "",
        "external_number": "",
        "internal_number": "",
        "was_recorded": False,
        "is_transferred": False,
        "mos_score": None,
        # --- timestamps (existing + new) ---
        "date_connected": "",
        "date_ended": "",
        "date_started": "",
        "date_rang": "",
        "event_timestamp": "",
        # --- durations (existing + new) ---
        "total_duration": 0,
        "duration": 0,
        # --- target (existing + new) ---
        "target_name": "",
        "target_type": "",
        "target_id": "",
        "target_phone": "",
        "target_email": "",
        # --- entry-point + proxy targets (new: often empty dicts) ---
        "entry_point_target": {},
        "proxy_target": {},
        # The master call_id is what the API is keyed by; entry_point_call_id
        # is the id Dialpad uses in the recording-page URL agents actually see.
        # For inbound→queue→agent flows they differ; for direct calls they may
        # match. Always returned so build_dialpad_link can route the user to
        # the page that exists in their Dialpad UI.
        "entry_point_call_id": "",
        # --- call-id family (new) ---
        "call_id": "",
        "master_call_id": "",
        "operator_call_id": "",
        # --- routing + state (new) ---
        "group_id": "",
        "state": "",
        # --- recording artifacts (new) ---
        "call_recording_ids": [],
        "recording_url": [],
        "recording_details": [],
        "screen_recording_urls": [],
        # --- forward-compat: full API payload ---
        "raw": {},
    }

    if not os.getenv("DIALPAD_API_KEY"):
        return empty

    try:
        async with _SEMAPHORE, httpx.AsyncClient() as client:
            resp = await client.get(
                f"{BASE_URL}/call/{call_id}",
                headers=_headers(),
                timeout=15.0,
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as e:
        status = e.response.status_code
        if status == 429:
            print(f"[dialpad_client] get_call_details rate-limited for {call_id} — metadata blank")
            raise DialpadRateLimited(f"429 on /call/{call_id}") from e
        print(f"[dialpad_client] get_call_details HTTP {status} for {call_id}: {e}")
        return empty
    except httpx.RequestError as e:
        print(f"[dialpad_client] get_call_details request error for {call_id}: {e}")
        return empty

    contact = data.get("contact", {}) or {}
    target = data.get("target", {}) or {}

    return {
        # --- caller (existing) ---
        "caller_name": contact.get("name", "") or "",
        "caller_phone": contact.get("phone", "") or data.get("external_number", "") or "",
        "caller_email": contact.get("email", "") or "",
        # --- caller (new) ---
        "contact_id": str(contact.get("id", "") or ""),
        "contact_type": contact.get("type", "") or "",
        # --- call routing (existing) ---
        "direction": data.get("direction", "") or "",
        "external_number": data.get("external_number", "") or "",
        "internal_number": data.get("internal_number", "") or "",
        "was_recorded": bool(data.get("was_recorded", False)),
        "is_transferred": bool(data.get("is_transferred", False)),
        "mos_score": data.get("mos_score"),
        # --- timestamps (existing + new) ---
        "date_connected": data.get("date_connected", "") or "",
        "date_ended": data.get("date_ended", "") or "",
        "date_started": data.get("date_started", "") or "",
        "date_rang": data.get("date_rang", "") or "",
        "event_timestamp": data.get("event_timestamp", "") or "",
        # --- durations (existing + new) ---
        "total_duration": data.get("total_duration", 0) or 0,
        "duration": data.get("duration", 0) or 0,
        # --- target (existing + new) ---
        "target_name": target.get("name", "") or "",
        "target_type": target.get("type", "") or "",
        "target_id": str(target.get("id", "") or ""),
        "target_phone": target.get("phone", "") or "",
        "target_email": target.get("email", "") or "",
        # --- routing-target dicts (new) ---
        "entry_point_target": data.get("entry_point_target", {}) or {},
        "proxy_target": data.get("proxy_target", {}) or {},
        "entry_point_call_id": str(data.get("entry_point_call_id", "") or ""),
        # --- call-id family (new) ---
        "call_id": str(data.get("call_id", "") or ""),
        "master_call_id": str(data.get("master_call_id", "") or ""),
        "operator_call_id": str(data.get("operator_call_id", "") or ""),
        # --- routing + state (new) ---
        "group_id": data.get("group_id", "") or "",
        "state": data.get("state", "") or "",
        # --- recording artifacts (new) ---
        "call_recording_ids": data.get("call_recording_ids", []) or [],
        "recording_url": data.get("recording_url", []) or [],
        "recording_details": data.get("recording_details", []) or [],
        "screen_recording_urls": data.get("screen_recording_urls", []) or [],
        # --- forward-compat: full API payload ---
        "raw": data,
    }


# ---------------------------------------------------------------------------
# Transcript + moments
# ---------------------------------------------------------------------------

def parse_transcript_payload(data: dict) -> dict:
    """
    Parse a GET /transcripts/{id} payload. Pure — testable without HTTP.

    Moment lines are occurrence markers: the moment TYPE arrives in
    `content` (`moment_type` is None in current payloads) and `name` is
    the AGENT, not the type. Markers carry no values, so none reach the
    scoring prompt; ALL of them are kept in `moments_display` for the
    frontend and Stage-1 persistence to `dialpad_call_metadata.moments`
    (filtering is a prompt decision, never a storage decision —
    DispositionDesign §2).
    """
    lines = data.get("lines", [])
    transcript_lines = []        # flat text for the prompt
    transcript_display = []      # structured list for the frontend
    moments_display = []         # full marker set: frontend + Stage-1 persistence
    call_start = None            # first timestamp, used to compute relative mm:ss

    def _mmss(ts_raw: str) -> str:
        try:
            ts_dt = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return ""
        if call_start is None:
            return ""
        elapsed = (ts_dt - call_start).total_seconds()
        mins, secs = divmod(int(elapsed), 60)
        return f"{mins}:{secs:02d}"

    for line in lines:
        line_type = line.get("type", "")
        ts_raw = line.get("time", "")

        if line_type == "transcript":
            name = line.get("name", "Unknown")
            content = line.get("content", "").strip()
            if not content:
                continue

            if ts_raw and call_start is None:
                try:
                    call_start = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    pass

            transcript_lines.append(f"{name}: {content}")
            transcript_display.append({
                "timestamp": _mmss(ts_raw) if ts_raw else "",
                "speaker": name,
                "text": content,
            })

        elif line_type in ("moment", "real_time_moment", "custom_moment"):
            moment_type = line.get("moment_type") or line.get("content", "").strip()
            if not moment_type:
                continue

            moments_display.append({
                "timestamp": _mmss(ts_raw) if ts_raw else "",
                "time": ts_raw,
                "type": moment_type,
                "agent": line.get("name", ""),
            })

    return {
        "transcript_text": "\n".join(transcript_lines),
        "transcript_display": transcript_display,
        "moments_display": moments_display,
    }


async def get_transcript(call_id: str) -> dict:
    """
    Fetch the full transcript for a call and parse it via
    `parse_transcript_payload`. On any failure (no API key, HTTP error,
    network error) returns empty shapes — scoring proceeds from audio only.
    """
    empty = {"transcript_text": "", "transcript_display": [], "moments_display": []}
    if not os.getenv("DIALPAD_API_KEY"):
        return empty

    try:
        async with _SEMAPHORE, httpx.AsyncClient() as client:
            response = await client.get(
                f"{BASE_URL}/transcripts/{call_id}",
                headers=_headers(),
                timeout=20,
            )
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPStatusError as e:
        print(f"[dialpad_client] Transcript fetch failed ({e.response.status_code}): {e}")
        return empty
    except httpx.RequestError as e:
        print(f"[dialpad_client] Transcript request error: {e}")
        return empty

    return parse_transcript_payload(data)


# ---------------------------------------------------------------------------
# Recording download (requires recordings_export scope on the API key)
# ---------------------------------------------------------------------------

async def download_recording(call_id: str) -> bytes:
    """
    Fetch the call recording audio bytes for `call_id`.

    Two-step: GET /call/{id} → extract recording_details[0].url,
    then GET that URL with apikey query param (Dialpad serves the audio
    via a signed-redirect).

    Raises:
      RuntimeError          — DIALPAD_API_KEY missing.
      NoRecordingAvailable  — call has no recording_details.
      DialpadRateLimited    — Dialpad returned 429 on either step.
      httpx.HTTPStatusError — any other non-2xx (401 = missing scope).
    """
    token = os.getenv("DIALPAD_API_KEY")
    if not token:
        raise RuntimeError("DIALPAD_API_KEY not set")

    async with _SEMAPHORE, httpx.AsyncClient(follow_redirects=True) as client:
        meta = await client.get(
            f"{BASE_URL}/call/{call_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        if meta.status_code == 429:
            raise DialpadRateLimited(f"429 on /call/{call_id}")
        meta.raise_for_status()

        details = meta.json().get("recording_details") or []
        if not details:
            raise NoRecordingAvailable(f"No recording_details for call {call_id}")
        recording_url = details[0].get("url")
        if not recording_url:
            raise NoRecordingAvailable(f"recording_details[0].url missing for call {call_id}")

        audio = await client.get(
            recording_url,
            params={"apikey": token},
            timeout=60,
        )
        if audio.status_code == 429:
            raise DialpadRateLimited(f"429 on recording fetch for {call_id}")
        audio.raise_for_status()
        return audio.content

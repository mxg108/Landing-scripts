"""
Dialpad API client.
Handles: user lookup, call listing by agent + timeframe, transcript fetching.
Recording download is stubbed — requires recordings_export scope (contact BI Manager).
"""

import os
from datetime import datetime
from typing import Optional
import httpx

BASE_URL = "https://dialpad.com/api/v2"
CALL_DURATION_FLAG_MS = 25 * 60 * 1000  # 25 minutes — flag for manager review

# Dialpad moment types to strip before passing to the scoring model (noise)
FILTERED_MOMENT_TYPES = {
    "whole_call_summary_fragment",
    "whole_call_summary",
    "ner",
    "action_item_v2",
    "ai_csat_reboot",
    "call_disposition",
    "call_purpose",
    "question",
}


def _headers() -> Optional[dict]:
    token = os.getenv("DIALPAD_API_KEY")
    if not token:
        return None  # caller checks for None and skips
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# User / agent lookup
# ---------------------------------------------------------------------------

async def get_user_id_by_name(agent_name: str) -> Optional[str]:
    """
    Resolve a human agent name to a Dialpad user_id.
    Matches case-insensitively on display_name or first+last name.
    Returns None if no match found.
    """
    async with httpx.AsyncClient() as client:
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


# ---------------------------------------------------------------------------
# Call listing
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

    async with httpx.AsyncClient() as client:
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


def build_dialpad_link(call_id: str) -> str:
    """Construct the Dialpad web link for a call."""
    return f"https://dialpad.com/callhistory/callreview/{call_id}"


# ---------------------------------------------------------------------------
# Transcript + moments
# ---------------------------------------------------------------------------

async def get_transcript(call_id: str) -> dict:
    """
    Fetch the full transcript for a call.
    Returns a dict with 'transcript_text' and 'moments_text' ready for the prompt.
    Returns empty strings if DIALPAD_API_KEY is not configured.
    """
    if not os.getenv("DIALPAD_API_KEY"):
        return {
            "transcript_text": "",
            "moments_text": "Dialpad not configured — transcript unavailable.",
        }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{BASE_URL}/transcripts/{call_id}",
                headers=_headers(),
                timeout=20,
            )
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPStatusError as e:
        print(f"[dialpad_client] Transcript fetch failed ({e.response.status_code}): {e}")
        return {
            "transcript_text": "",
            "moments_text": f"Transcript unavailable (HTTP {e.response.status_code}). Scoring from audio only.",
        }
    except httpx.RequestError as e:
        print(f"[dialpad_client] Transcript request error: {e}")
        return {
            "transcript_text": "",
            "moments_text": "Transcript unavailable (network error). Scoring from audio only.",
        }

    lines = data.get("lines", [])
    transcript_lines = []
    moments = []

    for line in lines:
        line_type = line.get("type", "")

        if line_type == "transcript":
            name = line.get("name", "Unknown")
            content = line.get("content", "").strip()
            if content:
                transcript_lines.append(f"{name}: {content}")

        elif line_type in ("moment", "real_time_moment", "custom_moment"):
            moment_type = line.get("moment_type") or line.get("name", "")
            if moment_type and moment_type not in FILTERED_MOMENT_TYPES:
                ts = line.get("time", "")
                moments.append(f"[{moment_type}] at {ts}")

    return {
        "transcript_text": "\n".join(transcript_lines),
        "moments_text": "\n".join(moments) if moments else "No relevant moments detected.",
    }


# ---------------------------------------------------------------------------
# Recording download (requires recordings_export scope — stubbed for now)
# ---------------------------------------------------------------------------

async def download_recording(call_id: str, dest_path: str) -> str:
    """
    Download call recording audio to dest_path.

    REQUIRES: recordings_export scope on the Dialpad API key.
    Contact your BI Manager to enable this scope.

    Until enabled, this raises NotImplementedError.
    Once the scope is confirmed, replace the raise with the implementation below.
    """
    raise NotImplementedError(
        "Recording auto-download requires the recordings_export API scope. "
        "Enable it via Admin Settings > Authentication > API Keys, "
        "or contact your BI Manager. In the meantime, upload audio files manually."
    )

    # --- Implementation (uncomment when scope is enabled) ---
    # import httpx, os
    # token = os.getenv("DIALPAD_API_KEY")
    # async with httpx.AsyncClient() as client:
    #     # Step 1: get recording URL from call details
    #     r = await client.get(f"{BASE_URL}/call/{call_id}", headers=_headers(), timeout=15)
    #     r.raise_for_status()
    #     details = r.json().get("recording_details", [])
    #     if not details:
    #         raise ValueError(f"No recording_details found for call {call_id}")
    #     recording_url = details[0]["url"]
    #
    #     # Step 2: download with apikey query param (required by Dialpad)
    #     audio = await client.get(
    #         recording_url,
    #         params={"apikey": token},
    #         headers={"bearer": token},
    #         follow_redirects=True,
    #         timeout=60,
    #     )
    #     audio.raise_for_status()
    #
    # with open(dest_path, "wb") as f:
    #     f.write(audio.content)
    # return dest_path

"""
Scoring pipeline orchestrator.
Ties together: Dialpad transcript -> Notion SOP -> Gemini scoring.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from backend.models.scorecard import ScorecardWithMeta
from backend.services.audio_service import score_audio
from backend.services.dialpad_client import get_transcript, get_call_details, build_dialpad_link, CALL_DURATION_FLAG_MS
from backend.services.notion_service import fetch_sop_for_call

if TYPE_CHECKING:
    from backend.config.team_config import TeamConfig


async def score_call(
    audio_bytes: bytes,
    filename: str,
    call_id: str,
    agent_name: str,
    manager_email: str,
    config: TeamConfig,
    duration_ms: float = 0,
) -> ScorecardWithMeta:
    """
    Full pipeline for one call:
    1. Fetch transcript + moments from Dialpad
    2. Fetch matching SOP from Notion
    3. Score with Gemini (audio + transcript + SOP)
    4. Return enriched scorecard
    """
    # Step 1: Dialpad transcript
    transcript_data = await get_transcript(call_id)
    transcript_text = transcript_data["transcript_text"]
    moments_text = transcript_data["moments_text"]

    # Step 2: Notion SOP
    sop_data = await fetch_sop_for_call(transcript_text)

    # Step 3: Score
    extra_notes = ""
    flagged_long = duration_ms > CALL_DURATION_FLAG_MS
    if flagged_long:
        extra_notes = (
            "NOTE: This call is over 25 minutes. Pay special attention to "
            "Efficiency & Call Handling (Section 8). Flag any unnecessary hold time or delays, as well"
            "as any associated timestamps. "
        )

    scorecard = await score_audio(
        audio_bytes=audio_bytes,
        filename=filename,
        config=config,
        transcript_text=transcript_text,
        moments_text=moments_text,
        sop_title=sop_data["sop_title"],
        sop_content=sop_data["sop_content"],
        agent_name=agent_name,
        extra_notes=extra_notes,
    )

    # Step 4: Caller metadata from Dialpad
    call_details = await get_call_details(call_id)

    return ScorecardWithMeta(
        **scorecard.model_dump(),
        call_id=call_id,
        agent_name=agent_name,
        manager_email=manager_email,
        dialpad_link=build_dialpad_link(call_id),
        duration_ms=duration_ms,
        flagged_long_call=flagged_long,
        sop_used=sop_data["sop_title"] or None,
        caller_name=call_details.get("caller_name", ""),
        caller_phone=call_details.get("caller_phone", ""),
        transcript_display=transcript_data.get("transcript_display", []),
        moments_display=transcript_data.get("moments_display", []),
    )

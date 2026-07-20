"""
Scoring pipeline orchestrator.
Ties together: Dialpad transcript -> Notion SOP -> Gemini scoring.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from backend.models.scorecard import ScorecardWithMeta
from backend.services.audio_service import score_audio
from backend.services.dialpad_client import (
    CALL_DURATION_FLAG_MS,
    _epoch_ms_to_utc_datetime,
    build_dialpad_link,
    get_call_details,
    get_transcript,
)
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
    transcript_data: Optional[dict] = None,
    call_details: Optional[dict] = None,
) -> ScorecardWithMeta:
    """
    Full pipeline for one call:
    1. Fetch transcript + moments from Dialpad (or use caller-supplied)
    2. Fetch matching SOP from Notion
    3. Score with Gemini (audio + transcript + SOP)
    4. Return enriched scorecard

    `transcript_data` and `call_details` may be pre-fetched by the route handler
    to avoid concurrent Dialpad bursts across fan-out background tasks.
    """
    # Step 1: Dialpad transcript
    if transcript_data is None:
        transcript_data = await get_transcript(call_id)
    transcript_text = transcript_data["transcript_text"]

    # Step 2: Notion SOP
    sop_data = await fetch_sop_for_call(transcript_text)

    # Step 3: Score
    extra_notes = ""
    flagged_long = duration_ms > CALL_DURATION_FLAG_MS
    if flagged_long:
        focus_ids = config.scoring_prompt.long_call_focus_sections
        focus_sections = [
            config.scoring_id_to_section[sid]
            for sid in focus_ids
            if sid in config.scoring_id_to_section
        ]
        if focus_sections:
            focus_text = ", ".join(
                f"{s.name} (Section {s.section_number})" for s in focus_sections
            )
            extra_notes = (
                f"NOTE: This call is over 25 minutes. Pay special attention to "
                f"{focus_text}. Flag any unnecessary hold time, delays, or "
                f"pacing issues, including timestamps. "
            )
        else:
            extra_notes = (
                "NOTE: This call is over 25 minutes. Pay special attention to "
                "audio-dependent sections. Flag any unnecessary hold time, "
                "delays, or pacing issues, including timestamps. "
            )

    scorecard = await score_audio(
        audio_bytes=audio_bytes,
        filename=filename,
        config=config,
        transcript_text=transcript_text,
        sop_title=sop_data["sop_title"],
        sop_content=sop_data["sop_content"],
        agent_name=agent_name,
        extra_notes=extra_notes,
    )

    # Step 4: Caller metadata from Dialpad
    if call_details is None:
        call_details = await get_call_details(call_id)

    return ScorecardWithMeta(
        **scorecard.model_dump(),
        call_id=call_id,
        agent_name=agent_name,
        manager_email=manager_email,
        dialpad_link=build_dialpad_link(call_id, call_details.get("entry_point_call_id", "")),
        duration_ms=duration_ms,
        flagged_long_call=flagged_long,
        sop_used=sop_data["sop_title"] or None,
        caller_name=call_details.get("caller_name", ""),
        caller_phone=call_details.get("caller_phone", ""),
        # Call-time initiative (PR-1) — see references/CallTimeOnAnalystHistory.md
        call_started_at_utc=_epoch_ms_to_utc_datetime(call_details.get("date_connected")),
        call_ended_at_utc=_epoch_ms_to_utc_datetime(call_details.get("date_ended")),
        transcript_display=transcript_data.get("transcript_display", []),
        moments_display=transcript_data.get("moments_display", []),
    )

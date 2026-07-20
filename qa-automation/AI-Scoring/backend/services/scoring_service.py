"""
Scoring pipeline orchestrator.
Ties together: Dialpad transcript -> CC call context -> Notion SOP ->
Gemini scoring.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

from backend.models.scorecard import ScorecardWithMeta
from backend.services.audio_service import score_audio
from backend.services.cc_context import (
    build_call_context_block,
    fetch_call_context,
    grounding_mode,
)
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

logger = logging.getLogger(__name__)


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

    # Step 1.5: CC call context (DispositionDesign §5). call_details moves
    # ahead of scoring because the triple-key match wants the entry-point
    # and master ids. Non-fatal throughout — an unmatched call scores
    # exactly as it does today.
    if call_details is None:
        call_details = await get_call_details(call_id)
    cc_ctx = None
    call_context_text = ""
    mode = grounding_mode()
    if mode != "off":
        cc_ctx = await fetch_call_context(
            config.team_id,
            entry_point_call_id=call_details.get("entry_point_call_id", ""),
            dialpad_call_id=call_id,
            master_call_id=call_details.get("master_call_id", ""),
        )
    if cc_ctx is not None:
        rendered = build_call_context_block(cc_ctx)
        if mode == "on":
            call_context_text = rendered
        else:
            # Shadow week: log-only compare — the block that WOULD have
            # been injected, prompt untouched.
            logger.info(
                "cc_grounding[shadow] call=%s matched_by=%s would inject:\n%s",
                call_id, cc_ctx.matched_by, rendered,
            )

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
        call_context_text=call_context_text,
    )

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
        # §5 step 4: Stage-1 stamps from the CC match (None when unmatched
        # or undispositioned — absence is a first-class state).
        dialpad_disposition_category=cc_ctx.disposition_category if cc_ctx else None,
        dialpad_disposition=cc_ctx.disposition if cc_ctx else None,
        ai_csat=cc_ctx.ai_csat if cc_ctx else None,
    )

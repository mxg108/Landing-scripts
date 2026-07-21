"""Scoring pipeline orchestrator — the full path of a scored call.

`score_call` below is the heart of the pipeline. This docstring traces
the WHOLE journey of the most common workflow — "Score Call" on the
/lookup page — because the human trigger is about to be replaced by an
automated process: everything DOWNSTREAM of the button is already
automation, so the replacement only needs to reproduce the POST in
step 2 and honor the constraints listed at the bottom.

1. ENTRY — /lookup (frontend/lookup.html)
   The operator searches an agent by email, sees their recent Dialpad
   calls, and clicks "Score Call" (`scoreCall()`). The button is gated
   by GET /lookup/scoring-permission (team keys need the agent in the
   team's Mails roster; privileged keys may score anyone and pick the
   target team via a modal when the agent is unrostered).

2. SUBMIT — POST /api/{team_id}/score (backend/routes/scoring.py,
   `score_single_call`)
   Form fields: call_id, agent_email, manager_email (no audio file —
   that is the legacy manual-upload mode). Synchronously, in order:
     - identity: resolve agent name<->email via the Mails roster;
     - auth: `check_scoring_access` (denials write a Score_Audit row);
     - idempotency: job_id = f(call_id, agent_name); a re-POST while a
       prior job is pending/scoring returns THAT job instead of
       double-scoring (double-click safe — keep this in the automated
       trigger);
     - audio: `download_recording(call_id)` from Dialpad (422 when the
       call has no recording, 503 on rate-limit);
     - prefetch: `get_transcript` + `get_call_details` here, NOT in the
       worker — fan-out background tasks would burst Dialpad into 429s;
     - audit: Score_Audit "scored" row before the worker is scheduled,
       so the action is logged even if the worker dies;
     - spawn the background `run()` and return {job_id, "pending"}.

3. SCORE — `score_call` (this module), inside a per-key semaphore:
     Step 1    transcript text from the prefetched payload (C0: Dialpad
               markers never reach the prompt; the full marker set rides
               `moments_display` into eval metadata).
     Step 1.5  CC grounding (DispositionDesign §5, cc_context.py):
               triple-key match (entry-point -> per-leg -> master)
               against command_center.calls, populated TODAY by the
               half-hour Stats pull (disposition_pull.py; webhook
               ingestion pending ops/engineering). Gated by
               CC_GROUNDING_MODE: off | shadow (default: stamp + log
               the would-be block, prompt untouched) | on (inject the
               "CALL CONTEXT (VERIFIED SYSTEM DATA)" block ahead of the
               transcript). Hold wording only for webhook-observed
               calls (has_hold_truth); Spanish calls follow the
               audio-is-SOT language rule (v2.1).
     Step 2    SOP context: `fetch_sop_for_call` — keyword classify ->
               Notion page text. INTERIM: the SopRag cascade (PR #99,
               open) replaces this with disposition-keyed retrieval
               over the coach-cards corpus after the Sandy re-platform.
     Step 3    `score_audio`: upload audio to Gemini, build the prompt
               (rubric + SOP + grounding + transcript, from TeamConfig
               JSON), parse/validate the Scorecard.
     Step 4    return ScorecardWithMeta: caller metadata, call clocks,
               dialpad_link (entry-point id preferred), and the CC
               stamps (dialpad_disposition*, ai_csat) for the eval row.

4. PERSIST — back in the route worker `run()`:
     - `record_draft_evaluation(strict=True)`: qa.evaluations draft +
       sections — the DB row is truth (§7.3 Phase C), so DB failure
       fails the JOB, deliberately;
     - `_postgres_post_stage1` (CutoverDesign §2): the human-review
       gate decides auto-finalize vs pause —
         CLEAN   -> `stamp_and_finalize` (engine score + formula/rubric
                    version stamps) -> Analyst_History projection ->
                    SSE `evaluation_finalized` -> GAS scorecard email.
                    Zero analyst touch.
         FLAGGED -> row stays draft (scoring_status=
                    'flagged_human_review'); an operator resolves it in
                    the red editor and approves
                    (POST /score/{job_id}/approve).

5. OBSERVE — the frontend polls GET /score/{job_id} every 3s and links
   to /scorecard/{team_id}/{job_id} (read-only when finalized, editor
   when flagged).

Constraints the automated trigger MUST keep in mind:
   - `_jobs` is in-process memory: poll the same instance that accepted
     the POST, and a restart forgets in-flight jobs (the eval row and
     sheet write survive; the job status does not).
   - Concurrency is capped per API key (semaphore) — queue, don't spray.
   - The Gemini call is the expensive step; idempotency (step 2) is the
     only guard against paying for it twice.
   - Score AFTER the half-hour stats pull has covered the call, or the
     prompt grounds without a disposition (absence path) — the eval's
     disposition columns are still filled by a later pull.
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
        # Durable join keys for the eval row — Dialpad id-spaces cross
        # (the Stats export keys by entry-point id), so the eval must
        # carry every id it was scored under, not just the per-leg one.
        dialpad_entry_point_call_id=call_details.get("entry_point_call_id") or None,
        dialpad_master_call_id=call_details.get("master_call_id") or None,
    )

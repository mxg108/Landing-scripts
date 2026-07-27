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
     Step 2    SOP context (PulpoConnection §4.2): disposition-keyed
               retrieval from the RAG provider (Pulpo), gated by
               PULPO_SOP_MODE (off | shadow | on). The legacy keyword→
               page path was decommissioned 2026-07-23; when
               retrieval is off/skipped/sub-τ the prompt takes the
               sop_context_missing conservative path.
     Step 2.5  Stage-A annotation (TwoStageScoringDesign §3), gated by
               SCORING_PIPELINE (single | annotate_only | two_stage*):
               annotate_audio produces the gemini_annotate_v1 artifact,
               persisted to qa.evaluations.annotated_transcript. In
               annotate_only the scoring prompt is untouched; Stage-B
               judging (two_stage*) lands with P3. Never a blocker.
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
import os
import time
from typing import TYPE_CHECKING, Optional

from backend.models.scorecard import ScorecardWithMeta
from backend.services.audio_service import annotate_audio, annotator_model, score_audio
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
from backend.services.judge_service import score_annotation
from backend.services.rag.sop_retrieval import fetch_sop_context, pulpo_sop_mode

if TYPE_CHECKING:
    from backend.config.team_config import TeamConfig

logger = logging.getLogger(__name__)

_PIPELINE_MODES = ("single", "annotate_only", "two_stage_shadow", "two_stage")


def scoring_pipeline() -> str:
    """SCORING_PIPELINE — TwoStageScoringDesign §6 (house pattern:
    unknown values collapse to the off-state).

      single           today's one-call Gemini scoring (default)
      annotate_only    Stage A runs alongside single-stage scoring; the
                       annotation persists, the prompt is untouched —
                       inspectable audio evidence accumulates before any
                       judge change
      two_stage_shadow single-stage result is SERVED; the Stage-B judge
                       also runs on the annotation and the compare stamp
                       rides dialpad_call_metadata.two_stage_shadow
      two_stage        the Stage-B judge is the score author; Stage
                       failures fall back to single-stage
                       (models_used.fallback records why)
    """
    mode = os.environ.get("SCORING_PIPELINE", "single").strip().lower()
    return mode if mode in _PIPELINE_MODES else "single"


def _shadow_sections(scorecard) -> dict:
    """Compact per-section view for the shadow stamp — enough for the
    report to compute agreement without re-parsing full scorecards."""
    return {
        s.id: {"score": s.score, "yn_value": s.yn_value, "confidence": s.confidence}
        for s in scorecard.sections
    }


async def _run_shadow_judge(
    annotation, config, primary_scorecard, *,
    sop_data, agent_name, extra_notes, call_context_text, call_id,
) -> dict:
    """Run the Stage-B judge in shadow (§6): compare against the served
    single-stage scorecard, log the disagreement summary, and return the
    stamp for dialpad_call_metadata. Never raises."""
    started = time.monotonic()
    try:
        shadow_sc, judge_result = await score_annotation(
            annotation,
            config,
            sop_title=sop_data["sop_title"],
            sop_content=sop_data["sop_content"],
            agent_name=agent_name,
            extra_notes=extra_notes,
            call_context_text=call_context_text,
        )
    except Exception as exc:  # noqa: BLE001 — shadow never blocks scoring
        logger.exception(
            "two_stage_shadow judge failed for call=%s — stamped as error",
            call_id,
        )
        return {
            "error": f"{type(exc).__name__}: {exc}"[:300],
            "elapsed_s": round(time.monotonic() - started, 1),
        }

    primary = _shadow_sections(primary_scorecard)
    shadow = _shadow_sections(shadow_sc)
    mismatches = [
        sid for sid, vals in shadow.items()
        if sid in primary
        and (vals["score"], vals["yn_value"])
        != (primary[sid]["score"], primary[sid]["yn_value"])
    ]
    logger.info(
        "two_stage_shadow call=%s judge=%s/%s: %d/%d sections disagree%s",
        call_id, judge_result.provider, judge_result.model,
        len(mismatches), len(shadow),
        f" ({', '.join(mismatches)})" if mismatches else "",
    )
    return {
        "scorer_provider": judge_result.provider,
        "scorer_model": judge_result.model,
        "sections": shadow,
        "mismatched_section_ids": mismatches,
        "key_strengths": shadow_sc.key_strengths,
        "opportunities": shadow_sc.opportunities,
        "elapsed_s": round(time.monotonic() - started, 1),
    }


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
    2. Retrieve SOP context from the RAG provider (PULPO_SOP_MODE-gated)
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

    # Step 2: SOP context — PulpoConnection §4.2/§4.3. The legacy
    # keyword→page path was decommissioned 2026-07-23 (P4); the RAG
    # provider is the only SOP source. PULPO_SOP_MODE:
    #   off     no retrieval; prompt takes the sop_context_missing path
    #   shadow  retrieval runs + logs + stamps provenance; the prompt
    #           still takes the conservative path (log-only compare)
    #   on      the rendered block IS the SOP context; empty/sub-τ
    #           retrieval falls through to sop_context_missing
    sop_mode = pulpo_sop_mode()
    sop_ctx = None
    if sop_mode != "off":
        sop_ctx = await fetch_sop_context(
            disposition_category=cc_ctx.disposition_category if cc_ctx else None,
            disposition=cc_ctx.disposition if cc_ctx else None,
            transcript_text=transcript_text,
        )
        if sop_mode == "shadow":
            logger.info(
                "pulpo_sop[shadow] call=%s query=%r reason=%r would inject %d docs: %s",
                call_id, sop_ctx.query, sop_ctx.skipped_reason,
                len(sop_ctx.provenance),
                [p["title"] for p in sop_ctx.provenance],
            )
    if sop_mode == "on" and sop_ctx is not None:
        sop_data = {"sop_title": sop_ctx.sop_title, "sop_content": sop_ctx.block_text}
    else:
        sop_data = {"sop_title": "", "sop_content": ""}

    # Step 2.5: Stage-A annotation (TwoStageScoringDesign §3). In
    # annotate_only the artifact persists while the scoring prompt stays
    # untouched; the two_stage modes judge from it (Step 3). Non-fatal
    # throughout: annotation is an enhancement, never a scoring blocker
    # (the cc_context doctrine) — a two_stage run without an annotation
    # falls back to single-stage scoring.
    pipeline = scoring_pipeline()
    annotation = None
    annotation_model = None
    if pipeline != "single":
        try:
            annotation_model = annotator_model()
            annotation = await annotate_audio(
                audio_bytes,
                filename,
                transcript_text=transcript_text,
                moments_display=transcript_data.get("moments_display", []),
                model=annotation_model,
            )
            logger.info(
                "stage_a[%s] call=%s model=%s: %d turns, %d holds, lang=%s",
                pipeline, call_id, annotation_model,
                len(annotation.turns), len(annotation.holds),
                annotation.language_detected,
            )
        except Exception:
            logger.exception(
                "stage_a annotate failed for call=%s — scoring proceeds "
                "without an annotation", call_id,
            )
            annotation = None

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

    # Step 3a — two_stage: the Stage-B judge IS the score author
    # (TwoStageScoringDesign §4); single-stage score_audio survives as
    # Plan B (§5) so a Stage failure never loses a scoring day.
    scorecard = None
    scorer_provider = "gemini"
    scorer_model = None
    pipeline_fallback_reason = None
    if pipeline == "two_stage":
        if annotation is None:
            pipeline_fallback_reason = "annotate_failed"
        else:
            try:
                scorecard, judge_result = await score_annotation(
                    annotation,
                    config,
                    sop_title=sop_data["sop_title"],
                    sop_content=sop_data["sop_content"],
                    agent_name=agent_name,
                    extra_notes=extra_notes,
                    call_context_text=call_context_text,
                )
                scorer_provider = judge_result.provider
                scorer_model = judge_result.model
                logger.info(
                    "two_stage call=%s judged by %s/%s",
                    call_id, scorer_provider, scorer_model,
                )
            except Exception:
                logger.exception(
                    "two_stage judge failed for call=%s — falling back to "
                    "single-stage scoring", call_id,
                )
                pipeline_fallback_reason = "text_scorer_failed"
        if pipeline_fallback_reason:
            logger.warning(
                "two_stage fallback (%s) for call=%s — models_used.fallback "
                "will record it", pipeline_fallback_reason, call_id,
            )

    # Step 3b — single-stage Gemini scoring: the primary in single /
    # annotate_only / two_stage_shadow, and two_stage's Plan B.
    if scorecard is None:
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

    # Step 3c — shadow judging (§6): the served result stays single-stage;
    # the judge runs on the same inputs and the compare stamp rides
    # dialpad_call_metadata.two_stage_shadow for the report. Never blocks.
    two_stage_shadow = None
    if pipeline == "two_stage_shadow" and annotation is not None:
        two_stage_shadow = await _run_shadow_judge(
            annotation, config, scorecard,
            sop_data=sop_data, agent_name=agent_name,
            extra_notes=extra_notes, call_context_text=call_context_text,
            call_id=call_id,
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
        # PulpoConnection §4.2 step 6 — retrieval provenance for the eval
        # row (stamped in shadow AND on: the shadow window's compare data).
        pulpo_docs=sop_ctx.provenance if sop_ctx else [],
        # TwoStageScoringDesign §3 — the Stage-A artifact + audio-leg
        # model stamp (eval_store fills models_used.audio from these).
        annotated_transcript=annotation.model_dump() if annotation else None,
        annotator_model=annotation_model if annotation else None,
        # §4 text-leg provenance + §5 fallback + §6 shadow stamp.
        # `model` identifies the score author; single-stage keeps its
        # (pre-existing) default when scorer_model is None.
        scorer_provider=scorer_provider,
        pipeline_fallback_reason=pipeline_fallback_reason,
        two_stage_shadow=two_stage_shadow,
        **({"model": scorer_model} if scorer_model else {}),
    )

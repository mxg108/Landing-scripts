"""FastAPI routes for the QA scoring pipeline.

Registered twice in main.py:
  - /api/{team_id}/...  (team-aware, TEAM_AUTH_DEPENDENCY)
  - /api/...            (legacy shim, resolves team_id='member_support')
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, UploadFile, File, Form

from backend.config import score_audit as audit_cfg
from backend.config.team_config import get_team_config
from backend.middleware.auth import (
    KeyIdentity,
    check_scoring_access,
    require_api_key,
    team_id_from_path,
)
from backend.models.scorecard import ApprovalRequest, OverrideRequest, RescoreRequest
from backend.services.history_service import (
    agent_email_for_name,
    agent_name_for_email,
    email_in_team_mails,
)
from backend.services import eval_store
from backend.services.eval_store import record_approval, record_draft_evaluation
from backend.services.sheets_projection import project_evaluation, tombstone_evaluation
from backend.services.stat_points import rebuild_agent_series
from backend.services.event_bus import get_event_bus
from backend.services.scoring_service import score_call
from backend.services.sheets_service import (
    append_score_audit_row,
    trigger_apps_script,
)
from backend.services.dialpad_client import (
    DialpadRateLimited,
    NoRecordingAvailable,
    download_recording,
    get_calls_for_agent,
    get_call_details,
    get_transcript,
    get_user_id_by_name,
)

router = APIRouter(tags=["scoring"])

# In-memory job store. Keyed by f"{team_id}:{job_id}" so jobs from one team
# cannot be read by another.
_jobs: dict[str, dict] = {}

# Per-API-key concurrent-jobs semaphore. Keyed on KeyIdentity (frozen
# dataclass, hashable) so two requests with the same key share a slot
# pool. Acquired inside the background task — the HTTP response still
# returns immediately, but the Gemini scoring call queues behind the
# cap so a single privileged operator can't fan out 50 calls at once.
_KEY_CONCURRENT_LIMIT = 5
_key_semaphores: dict[KeyIdentity, asyncio.Semaphore] = {}


def _semaphore_for_key(identity: KeyIdentity) -> asyncio.Semaphore:
    sem = _key_semaphores.get(identity)
    if sem is None:
        sem = asyncio.Semaphore(_KEY_CONCURRENT_LIMIT)
        _key_semaphores[identity] = sem
    return sem


def _job_key(team_id: str, job_id: str) -> str:
    return f"{team_id}:{job_id}"


def _make_job_id(call_id: str, agent_name: str) -> str:
    return f"{call_id}_{agent_name}".replace(" ", "_")


async def _job_from_db(team_id: str, job_id: str) -> Optional[dict]:
    """Reconstruct an approval context from qa.evaluations when the
    in-memory job is gone — server restart, old scorecard URL, or the
    /datapoints surface (ScorecardActionsDesign §3, slice S1).

    Concurrency: last-writer-wins by design. qa.evaluations has no
    updated_at column and these are low-traffic manager tools. If Landing
    outgrows this (concurrent evaluators on one eval), add updated_at +
    an If-Unmodified-Since-style optimistic check here — potential design
    choice deliberately deferred, not overlooked.
    """
    ref = await eval_store.resolve_evaluation(team_id, job_id)
    if ref is None:
        return None
    return _job_from_ref(ref)


def _ref_call_id(ref) -> str:
    """The best Dialpad call id for a resolved eval: the verified id
    columns first, then the dialpad_link's trailing segment — rows
    resolved via the link-tail arm (2026-07-27) may have NULL/foreign id
    columns, and audio download + audit rows still need an id."""
    return (
        ref.dialpad_call_id or ref.dialpad_entry_point_call_id
        or eval_store.call_id_from_dialpad_link(ref.dialpad_link)
    )


def _job_from_ref(ref) -> dict:
    """EvalRef → restored-job dict (the S1 reconstruction shape)."""
    return {
        "status": "complete",
        "state": ref.state,
        "scoring_status": ref.scoring_status,
        "evaluation_id": ref.id,
        "call_id": _ref_call_id(ref),
        "agent_name": ref.agent_name_raw,
        "agent_email": ref.agent_email or "",
        "overall_score": ref.overall_score,
        "restored_from_db": True,
        "scorecard": {
            "sections": ref.sections,
            "manager_email": ref.evaluator_email or "",
            "dialpad_link": ref.dialpad_link,
            "call_summary": ref.call_summary or "",
            "key_strengths": ref.key_strengths or "",
            "opportunities": ref.opportunities or "",
            "agent_name": ref.agent_name_raw,
            "model": ref.model,
        },
    }


def _call_in_flight(team_id: str, call_ids: set[str]) -> Optional[str]:
    """Whether any live job for this team is actively scoring one of the
    given Dialpad call ids. The scorecard and datapoint surfaces key jobs
    differently (call_id_agent vs bare call id), so an in-flight check by
    key alone misses cross-surface races — this scans job payloads."""
    prefix = f"{team_id}:"
    for k, j in _jobs.items():
        if (k.startswith(prefix)
                and j.get("call_id") in call_ids
                and j.get("status") in {"pending", "scoring"}):
            return j.get("status")
    return None


async def _await_coro(coro) -> None:
    """BackgroundTasks.add_task takes a callable + args, not a coroutine
    object — this adapts the S5 auto-rescore pass for scheduling."""
    await coro


async def _append_audit_row_threaded(**kwargs):
    """Score_Audit is a sync gspread append — run it in a worker thread
    so the Sheets round-trip never pins the event loop (2026-07-29
    incident: sync I/O inside async paths froze webhooks, pages, and SSE
    for the duration). Exceptions propagate unchanged — where the audit
    append blocks an action, that's policy, not an accident."""
    return await asyncio.to_thread(append_score_audit_row, **kwargs)


async def _dispatch_finalize_email(job, history_row, team_id, evaluation_id,
                                   default_disclaimer=None, suppress=False) -> None:
    """Apps Script email dispatch at the finalize seam — shared by the
    auto-flow and the approve path.

    S4 (ScorecardActionsDesign §4.2.7): a job carrying a ``rescore``
    context re-sends WITH the cause-specific disclaimer by default;
    ``suppress_email`` opts out and leaves a visible 'suppressed' receipt
    in the job payload. The context lives on the in-memory job only — a
    restart between rescore and re-approve loses it, and the re-finalize
    email goes out disclaimer-less (accepted at current traffic; the
    audit 'rescored' row still records the intervention).

    ``default_disclaimer`` (S6): the caller's cause tag when no rescore
    context rides the job — e.g. 'review_resolution' on a flagged-review
    approve. A rescore context wins over it. ``suppress`` (S7): the
    caller's own opt-out — the edit-of-finalized §8.1 checkbox."""
    if not history_row or history_row <= 0:
        job["email_dispatch"] = {
            "status": "skipped", "message": "no Analyst_History row projected",
        }
        return
    if suppress:
        job["email_dispatch"] = {
            "status": "suppressed",
            "message": "caller requested suppress_email",
        }
        return
    rescore = job.get("rescore") or {}
    if rescore.get("suppress_email"):
        job["email_dispatch"] = {
            "status": "suppressed",
            "message": f"rescore ({rescore.get('cause', '?')}) requested suppress_email",
        }
        return
    disclaimer = f"rescore_{rescore['cause']}" if rescore else default_disclaimer
    try:
        # Sync httpx.post with a 60s timeout — threaded so the Apps
        # Script round-trip never pins the event loop.
        job["email_dispatch"] = await asyncio.to_thread(
            trigger_apps_script, history_row, team_id, disclaimer=disclaimer
        )
    except Exception as e:
        # Visible, not swallowed-into-print-limbo: the job payload
        # carries the failure so operators see it in the UI/logs.
        job["email_dispatch"] = {"status": "error", "message": str(e)}
        logging.getLogger(__name__).exception(
            "finalize email dispatch failed for eval %s (retryable)",
            evaluation_id,
        )


def _sse_eval_id_from_link(link: str) -> str:
    """Trailing path segment of dialpad_link — the /datapoint route key
    (mirrors team_stats._parse_row; see the legacy approve path)."""
    return eval_store.call_id_from_dialpad_link(link)


async def _publish_finalized_event(
    team_id, job, *, agent_name, evaluator_email, overall_score,
    history_row, dialpad_link, call_summary, key_strengths, opportunities,
):
    """SSE 'eval_approved' for the postgres paths (auto-finalize + flagged
    resolution) — same payload the legacy approve publishes, so dashboards
    toast identically. Never breaks the flow."""
    def _truncate(s: str, n: int = 280) -> str:
        s = s or ""
        return s if len(s) <= n else s[: n - 1] + "…"

    try:
        await get_event_bus().publish(team_id, "eval_approved", {
            "call_id": job.get("call_id", ""),
            "eval_id": _sse_eval_id_from_link(dialpad_link) or job.get("call_id", ""),
            "history_row": history_row,
            "agent": agent_name or "",
            "evaluator_email": evaluator_email,
            "overall_score": overall_score,
            "summary": _truncate(call_summary),
            "strengths": _truncate(key_strengths),
            "opportunities": _truncate(opportunities),
            "dialpad_link": dialpad_link or "",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as e:
        print(f"[sse] eval_approved publish failed (non-fatal): {e}")


async def _postgres_post_stage1(job, evaluation_id, scorecard, config, team_id,
                                *, key=None, api_key_role="", semaphore=None):
    """Post-flip auto-flow (CutoverDesign §2): after the Stage-1 draft row
    lands, decide auto-finalize vs. human-review pause.

    CLEAN → engine computes + stamps versions, row finalizes, Analyst_History
    is projected from the DB row, the GAS email fires — zero analyst touch.
    FLAGGED → mode-dependent (ScorecardActionsDesign §0.3, S5):
    ``authoritative`` (default) holds the row in draft at
    scoring_status='flagged_human_review' until an analyst resolves it;
    ``informative`` finalizes and ships it like any other row, keeping
    human_review_required_at as the queue marker.

    S5 auto-rescore hook (§4.2): a finalize landing ≤ config.rescore
    .threshold on a source='ai' row with the auto_rescored_at latch
    unstamped takes the machine's one retry — this pass's projection,
    SSE toast, and email are SUPPRESSED (the agent must not see a score
    about to change) and the rescore pass is returned as a coroutine for
    the CALLER to run (callers already inside a background task await it
    after closing their own job bookkeeping; the approve route schedules
    it via BackgroundTasks). A still-low score with the latch already
    stamped enters the review queue instead (marker stamp; the coaching
    → override escalation cue).

    Job payload gains state/scoring_status/overall_score so the lookup UI can
    color the button (CutoverDesign §5). Returns the rescore coroutine, or
    None when the flow completed here."""
    flagged = eval_store.human_review_trigger_fired(
        eval_store._active_formula(team_id), scorecard.sections
    ) or eval_store.requires_analyst_review(config)
    if flagged and config.human_review.mode == "authoritative":
        job["state"] = "draft"
        job["scoring_status"] = "flagged_human_review"
        return None

    detail = await eval_store.stamp_and_finalize(
        evaluation_id, config, evaluator_email=scorecard.manager_email or ""
    )
    score = float(detail.overall_score)
    job["state"] = "finalized"
    job["scoring_status"] = "complete"
    job["overall_score"] = score

    followup = await _maybe_auto_rescore(
        job, evaluation_id, score, config, team_id,
        key=key, api_key_role=api_key_role, semaphore=semaphore,
        manager_email=scorecard.manager_email or "",
    )
    if followup is not None:
        return followup

    if flagged:
        # Informative mode (§0.3): the row shipped; the queue marker is
        # the annotation. Insurance stamp — build_draft_row set required_at
        # for §3.14 triggers, but requires_analyst_review-only teams
        # never got one.
        await eval_store.mark_human_review_required(evaluation_id)
        job["human_review_queued"] = True

    pool = await eval_store.get_pool()
    history_row = await project_evaluation(
        pool, evaluation_id, config, include_history=True
    )
    job["history_row"] = history_row
    await _publish_finalized_event(
        team_id, job,
        agent_name=scorecard.agent_name,
        evaluator_email=scorecard.manager_email or "",
        overall_score=score,
        history_row=history_row,
        dialpad_link=scorecard.dialpad_link,
        call_summary=scorecard.call_summary,
        key_strengths=scorecard.key_strengths,
        opportunities=scorecard.opportunities,
    )
    await _dispatch_finalize_email(job, history_row, team_id, evaluation_id)
    return None


async def _maybe_auto_rescore(job, evaluation_id, score, config, team_id,
                              *, key, api_key_role, semaphore, manager_email):
    """The §4.2 finalize-seam decision. Returns the auto-rescore pass as
    an un-started coroutine when it fires (latch won), else None — and
    handles the still-low-after-retry queue entry as a side effect.

    Eligibility: score ≤ threshold AND source='ai' (a human-approved low
    score is a human's judgment — never discard edits; escalation for
    those is the S6 override) AND the once-EVER latch is unstamped. The
    latch stamp is atomic and happens BEFORE the pass is scheduled, so a
    crash mid-rescore can't loop (§4.2 "once means once")."""
    threshold = config.rescore.threshold
    if score > threshold:
        return None
    state = await eval_store.fetch_auto_rescore_state(evaluation_id)
    if state is None:
        return None

    if state["auto_rescored_at"] is not None:
        # The machine already took its one retry; a still-low score is
        # evidence about the call (or the SOP context), not a retry cue.
        await eval_store.mark_human_review_required(evaluation_id)
        job["human_review_queued"] = True
        return None
    if state["source"] != "ai":
        return None
    if key is None or semaphore is None:
        # Defensive: a caller that can't host the follow-up pass (none
        # today) must not strand a stamped latch.
        logging.getLogger(__name__).error(
            "auto-rescore for eval %s skipped: no job key/semaphore", evaluation_id)
        return None
    if not await eval_store.stamp_auto_rescored(evaluation_id):
        return None  # lost the race — exactly one retry, someone else's

    call_id = (
        state["dialpad_call_id"] or state["dialpad_entry_point_call_id"]
        or job.get("call_id") or ""
    )
    # Machine-initiated: NOT tampering (§0.2) — audit row with the
    # triggering key's role, no evaluator, no coaching receipt. Same
    # superseded_model stamp as the S4 manual path (P3+ traceability).
    await _append_audit_row_threaded(
        api_key_role=api_key_role,
        evaluator_email="",
        agent_email=state["agent_email"] or "",
        agent_name=state["agent_name_raw"],
        call_id=call_id,
        target_team=team_id,
        action=audit_cfg.ACTION_RESCORED,
        result_row=None,
        notes=f"auto: {score} ≤ {threshold}; superseded_model={state['model']}",
    )
    job["auto_rescore"] = "scheduled"

    async def followup():
        _jobs[key] = {
            "status": "pending",
            "call_id": call_id,
            "agent_email": state["agent_email"] or "",
            "agent_name": state["agent_name_raw"],
            "manager_email": manager_email,
            "rescore": {
                "cause": "auto",
                "suppress_email": False,
                "superseded_score": score,
                "superseded_model": state["model"],
            },
        }
        try:
            audio_bytes = await download_recording(call_id)
            transcript_data = await get_transcript(call_id)
            try:
                call_details = await get_call_details(call_id)
            except DialpadRateLimited:
                call_details = None
            await _rescore_core(
                key=key, team_id=team_id, config=config, call_id=call_id,
                agent_name=state["agent_name_raw"], manager_email=manager_email,
                duration_ms=float(state["duration_ms"] or 0),
                expected_row_id=evaluation_id, agent_id=state["agent_id"],
                semaphore=semaphore, api_key_role=api_key_role,
                audio_bytes=audio_bytes, transcript_data=transcript_data,
                call_details=call_details,
            )
        except Exception as e:
            # Latch already stamped — a dead pass can't loop; the audit
            # 'rescored' row + this error are the operator's cue to use
            # the S4 manual rescore.
            _jobs[key]["status"] = "error"
            _jobs[key]["error"] = str(e)

    return followup()


@router.get("/calls")
async def list_calls(agent_name: str, date_start: str, date_end: str):
    """
    Look up calls for an agent within a date range.
    Returns call list so the manager can confirm which calls to score.

    date_start / date_end: ISO format YYYY-MM-DD
    """
    try:
        start = datetime.fromisoformat(date_start)
        end = datetime.fromisoformat(date_end)
    except ValueError:
        raise HTTPException(status_code=400, detail="Dates must be YYYY-MM-DD format")

    user_id = await get_user_id_by_name(agent_name)
    if not user_id:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_name}' not found in Dialpad")

    calls = await get_calls_for_agent(user_id, start, end)

    return {
        "agent_name": agent_name,
        "user_id": user_id,
        "call_count": len(calls),
        "calls": [
            {
                "call_id": c.get("call_id"),
                "date_connected": c.get("date_connected"),
                "duration_ms": c.get("duration"),
                "was_recorded": c.get("was_recorded"),
                "flagged_long_call": c.get("_flagged_long_call"),
                "contact_name": c.get("contact", {}).get("name"),
            }
            for c in calls
        ],
    }


@router.post("/score")
async def score_single_call(
    request: Request,
    background_tasks: BackgroundTasks,
    identity: KeyIdentity = Depends(require_api_key),
    audio_file: Optional[UploadFile] = File(default=None),
    call_id: str = Form(...),
    agent_email: Optional[str] = Form(default=None),
    agent_name: Optional[str] = Form(default=None),
    manager_email: str = Form(...),
    duration_ms: float = Form(default=0),
):
    """Score a single call.

    Two input modes:
      * **Manual upload** (legacy) — caller sends ``audio_file`` + ``agent_name``.
      * **Lookup-driven** (new) — caller sends ``call_id`` + ``agent_email``;
        the backend downloads the recording via ``download_recording`` and
        resolves ``agent_name`` from the team's Mails roster.

    Returns ``{job_id, status}``. Idempotent against double-clicks: a
    second POST with the same ``(team_id, call_id, agent_name)`` while a
    prior job is still ``pending``/``scoring`` returns that job's id
    instead of starting a new run.

    Auth notes:
      * Team key: ``agent_email`` (or the email resolved from
        ``agent_name``) must be in the team's Mails roster.
      * Privileged key: any real ``team_id`` is allowed; roster
        membership is NOT required (caller picks the target team via the
        frontend's team-pick dialog when the agent is unrostered).
    """
    team_id = team_id_from_path(request)
    config = get_team_config(team_id)

    if not agent_email and not agent_name:
        raise HTTPException(
            status_code=400,
            detail="Must supply agent_email or agent_name",
        )

    # Identity resolution. Both directions are best-effort via Mails:
    #   - email supplied → resolve name (so audit logs the canonical
    #     display name from Mails col D, not whatever the form sent).
    #   - name supplied (legacy upload flow) → resolve email (needed by
    #     the roster check below — without it, a team key always 403s).
    if agent_email:
        resolved_name = await agent_name_for_email(agent_email, team_id)
        if resolved_name and not agent_name:
            agent_name = resolved_name
    elif agent_name:
        agent_email = await agent_email_for_name(agent_name, team_id)

    if not agent_name:
        # Unrostered + privileged caller may legitimately have only an
        # email — fall back to the local part so the job key stays stable.
        agent_name = (agent_email or "").split("@", 1)[0] or "unknown"

    # Auth: precompute roster membership (async I/O) then run the sync
    # check. On denial, write a denied-audit row before raising.
    is_in_roster = (
        await email_in_team_mails(agent_email, team_id)
        if agent_email else False
    )
    try:
        check_scoring_access(
            identity, team_id, agent_email, is_in_roster=is_in_roster,
        )
    except HTTPException as exc:
        await _append_audit_row_threaded(
            api_key_role=identity.role,
            evaluator_email=manager_email,
            agent_email=agent_email or "",
            agent_name=agent_name,
            call_id=call_id,
            target_team=team_id,
            action=audit_cfg.ACTION_DENIED,
            result_row=None,
            notes=f"http_{exc.status_code}",
        )
        raise

    # Idempotency: return an existing in-flight job_id rather than
    # launching a duplicate background run on double-click.
    job_id = _make_job_id(call_id, agent_name)
    key = _job_key(team_id, job_id)
    existing = _jobs.get(key)
    if existing and existing.get("status") in {"pending", "scoring"}:
        return {"job_id": job_id, "status": existing["status"]}

    # Resolve audio bytes — either uploaded or fetched from Dialpad.
    audio_bytes: bytes
    filename: str
    if audio_file is not None:
        audio_bytes = await audio_file.read()
        filename = audio_file.filename or f"{call_id}.mp3"
    else:
        try:
            audio_bytes = await download_recording(call_id)
        except NoRecordingAvailable:
            await _append_audit_row_threaded(
                api_key_role=identity.role,
                evaluator_email=manager_email,
                agent_email=agent_email or "",
                agent_name=agent_name,
                call_id=call_id,
                target_team=team_id,
                action=audit_cfg.ACTION_DENIED,
                result_row=None,
                notes="no_recording",
            )
            raise HTTPException(
                status_code=422,
                detail="Call has no recording in Dialpad",
            )
        except DialpadRateLimited:
            await _append_audit_row_threaded(
                api_key_role=identity.role,
                evaluator_email=manager_email,
                agent_email=agent_email or "",
                agent_name=agent_name,
                call_id=call_id,
                target_team=team_id,
                action=audit_cfg.ACTION_DENIED,
                result_row=None,
                notes="rate_limited",
            )
            raise HTTPException(
                status_code=503,
                detail="Dialpad rate-limited; retry shortly",
            )
        filename = f"{call_id}.mp3"

    _jobs[key] = {
        "status": "pending",
        "call_id": call_id,
        # Persisted so /score/{job_id}/approve can write a complete
        # Score_Audit row without re-deriving identity from the scorecard.
        "agent_email": agent_email or "",
        "agent_name": agent_name,
        "manager_email": manager_email,
    }

    # Pre-fetch Dialpad metadata in the handler (sequential per request) so
    # fan-out background tasks don't burst Dialpad and lose metadata to 429s.
    transcript_data = await get_transcript(call_id)
    try:
        call_details = await get_call_details(call_id)
    except DialpadRateLimited:
        print(f"[score] Dialpad rate-limited fetching call_details for {call_id}; proceeding with blanks")
        call_details = None

    # Audit row written before the background task is scheduled so the
    # operator action is logged even if the worker crashes.
    await _append_audit_row_threaded(
        api_key_role=identity.role,
        evaluator_email=manager_email,
        agent_email=agent_email or "",
        agent_name=agent_name,
        call_id=call_id,
        target_team=team_id,
        action=audit_cfg.ACTION_SCORED,
        result_row=None,
        notes="",
    )

    semaphore = _semaphore_for_key(identity)

    async def run():
        try:
            _jobs[key]["status"] = "scoring"
            async with semaphore:
                scorecard = await score_call(
                    audio_bytes=audio_bytes,
                    filename=filename,
                    call_id=call_id,
                    agent_name=agent_name,
                    manager_email=manager_email,
                    config=config,
                    duration_ms=duration_ms,
                    transcript_data=transcript_data,
                    call_details=call_details,
                )
            # Stage 1 (§7.3 Phase C): the DB row is truth — DB errors
            # raise and the whole job errors. The FR-AI draft projection
            # that used to precede this write was retired 2026-07-20:
            # the tab hit its grid limit and a deprecated Sheets write
            # took the whole pipeline down with it. Sheets writes are
            # now Analyst_History (finalize projection) + Score_Audit
            # ONLY.
            evaluation_id = await record_draft_evaluation(
                scorecard, config, strict=True
            )
            _jobs[key]["evaluation_id"] = evaluation_id
            _jobs[key]["scorecard"] = scorecard.model_dump()
            followup = None
            if evaluation_id is not None:
                followup = await _postgres_post_stage1(
                    _jobs[key], evaluation_id, scorecard, config, team_id,
                    key=key, api_key_role=identity.role, semaphore=semaphore,
                )
            _jobs[key]["status"] = "complete"
            if followup is not None:
                # S5 auto-rescore fired: this job's books are closed; the
                # pass replaces _jobs[key] and runs its own lifecycle.
                await followup
        except Exception as e:
            _jobs[key]["status"] = "error"
            _jobs[key]["error"] = str(e)

    background_tasks.add_task(run)
    return {"job_id": job_id, "status": "pending"}


@router.get("/score/{job_id}")
async def get_score_result(request: Request, job_id: str):
    """Poll for the result of a scoring job.

    S7: on a `_jobs` miss, restore the context from qa.evaluations (the
    §3 seam) and cache it — the scorecard page now renders ANY
    evaluation forever, not just jobs the current process scored. The
    datapoint page links here as the mirrored edit surface (§4.1)."""
    team_id = team_id_from_path(request)
    key = _job_key(team_id, job_id)
    job = _jobs.get(key)
    if not job:
        job = await _job_from_db(team_id, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        _jobs[key] = job
    return job


@router.get("/whoami")
async def whoami(identity: KeyIdentity = Depends(require_api_key)):
    """Resolved identity of the presented API key — lets the frontend
    role-gate controls (S7: the Delete button renders for privileged
    keys only). Rendering is UX; the routes still enforce."""
    return {"role": identity.role, "team_id": identity.team_id}


@router.post("/score/batch")
async def score_batch(
    request: Request,
    background_tasks: BackgroundTasks,
    identity: KeyIdentity = Depends(require_api_key),
    audio_files: list[UploadFile] = File(...),
    call_ids: str = Form(...),        # comma-separated, matching order of audio_files
    agent_name: str = Form(...),
    manager_email: str = Form(...),
    durations_ms: str = Form(default=""),  # comma-separated, matching order
):
    """Score multiple calls in one submission.

    ``audio_files`` and ``call_ids`` must be in the same order. Each row
    in the batch goes through the same plumbing as a single ``/score``
    POST: an in-flight (``pending``/``scoring``) job for the same key
    short-circuits (no duplicate scheduling, no second audit row), a
    Score_Audit row is appended before the background task is scheduled,
    and the Gemini ``score_call`` await runs inside the per-``KeyIdentity``
    concurrent-jobs semaphore.

    Unlike ``/score``, ``audio_files`` is still required — the batch
    upload form is the path that provides files directly. The Dialpad
    download fallback is single-call only (``/score``).

    Roster check: ``/score/batch`` does NOT enforce the team-key roster
    membership rule that ``/score`` does. The form has no ``agent_email``
    field; tightening this would change the upload UI contract. Audit
    rows still capture the operator's key role + chosen agent_name.
    """
    team_id = team_id_from_path(request)
    id_list = [cid.strip() for cid in call_ids.split(",")]
    dur_list = [float(d.strip()) if d.strip() else 0 for d in durations_ms.split(",")] if durations_ms else []

    if len(audio_files) != len(id_list):
        raise HTTPException(
            status_code=400,
            detail=f"Mismatch: {len(audio_files)} files vs {len(id_list)} call IDs"
        )

    config = get_team_config(team_id)
    semaphore = _semaphore_for_key(identity)
    job_ids: list[str] = []
    for i, (audio_file, call_id) in enumerate(zip(audio_files, id_list)):
        duration = dur_list[i] if i < len(dur_list) else 0
        job_id = _make_job_id(call_id, agent_name)
        key = _job_key(team_id, job_id)
        job_ids.append(job_id)

        # Idempotency — a row that already has an in-flight job reuses
        # that job_id, no new background task, no second audit row.
        existing = _jobs.get(key)
        if existing and existing.get("status") in {"pending", "scoring"}:
            continue

        audio_bytes = await audio_file.read()
        _jobs[key] = {
            "status": "pending",
            "call_id": call_id,
            # Persisted so /score/{job_id}/approve can write a complete
            # audit row (batch entries don't carry an agent_email).
            "agent_email": "",
            "agent_name": agent_name,
            "manager_email": manager_email,
        }

        await _append_audit_row_threaded(
            api_key_role=identity.role,
            evaluator_email=manager_email,
            agent_email="",
            agent_name=agent_name,
            call_id=call_id,
            target_team=team_id,
            action=audit_cfg.ACTION_SCORED,
            result_row=None,
            notes="batch_upload",
        )

        async def run(ab=audio_bytes, fn=audio_file.filename, cid=call_id, k=key, dur=duration):
            try:
                _jobs[k]["status"] = "scoring"
                async with semaphore:
                    scorecard = await score_call(
                        audio_bytes=ab,
                        filename=fn,
                        call_id=cid,
                        agent_name=agent_name,
                        manager_email=manager_email,
                        config=config,
                        duration_ms=dur,
                    )
                evaluation_id = await record_draft_evaluation(
                    scorecard, config, strict=True
                )
                _jobs[k]["evaluation_id"] = evaluation_id
                _jobs[k]["scorecard"] = scorecard.model_dump()
                followup = None
                if evaluation_id is not None:
                    followup = await _postgres_post_stage1(
                        _jobs[k], evaluation_id, scorecard, config, team_id,
                        key=k, api_key_role=identity.role, semaphore=semaphore,
                    )
                _jobs[k]["status"] = "complete"
                if followup is not None:
                    await followup
            except Exception as e:
                _jobs[k]["status"] = "error"
                _jobs[k]["error"] = str(e)

        background_tasks.add_task(run)

    return {"job_ids": job_ids, "count": len(job_ids)}


@router.post("/score/{job_id}/approve")
async def approve_scorecard(
    request: Request,
    job_id: str,
    approval: ApprovalRequest,
    background_tasks: BackgroundTasks,
    identity: KeyIdentity = Depends(require_api_key),
):
    """
    Manager approves a scored call (optionally with edits).

    Engine path (CutoverDesign §2/§5): DB state transition + engine
    compute + Sheets projection, then the Apps Script email dispatch.
    No destination tab, no ARRAYFORMULA readback — DB errors are hard
    errors (§7.3 Phase C). The pre-cutover four-stage sheet flow was
    deleted in the §8 slice-5 cleanup.

    Writes a Score_Audit row with action="approved" once the evaluation
    finalizes — captured before the Apps Script email dispatch so an
    Apps Script failure still leaves the audit trail intact.

    S5 (ScorecardActionsDesign §4.2): the approve path is a finalize
    seam, so the auto-rescore hook runs here too — an unedited approval
    (source still 'ai') landing ≤ threshold with the latch unstamped
    suppresses projection/email and schedules the machine's one retry
    via BackgroundTasks; the response says so (`auto_rescore`). An
    edited approval flips source to 'ai_reviewed' and never fires.
    """
    team_id = team_id_from_path(request)
    key = _job_key(team_id, job_id)
    job = _jobs.get(key)
    if not job:
        # S1 fallback (ScorecardActionsDesign §3): the in-memory job died
        # with the process, but the evaluation didn't — reconstruct the
        # approval context from the DB row and cache it like a live job.
        job = await _job_from_db(team_id, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        _jobs[key] = job
    return await _approve_core(
        team_id=team_id, key=key, job=job, approval=approval,
        identity=identity, background_tasks=background_tasks,
    )


async def _approve_core(*, team_id, key, job, approval, identity,
                        background_tasks, edit_of_finalized=False,
                        rebuild_agent_id=None, suppress_email=False):
    """The shared approval engine — scorecard approve and the S7
    datapoint edit surface run the same flow (§4.1: extend, don't
    rebuild). ``edit_of_finalized`` is the one behavioral fork: the
    finalized read-only wall is bypassed (the caller already enforced
    the §4.3a acknowledgment), the re-approval books the edit coaching
    receipt, the stat series is rebuilt after the re-finalize (a
    mid-chain score change invalidates every later EWMA point), the
    email carries the edit_finalized disclaimer, and §8.1
    ``suppress_email`` is honored."""
    if job["status"] == "approved" and not edit_of_finalized:
        raise HTTPException(status_code=409, detail="Already approved")
    if job["status"] not in ("complete", "approved"):
        raise HTTPException(
            status_code=409,
            detail=f"Job is '{job['status']}', must be 'complete' to approve",
        )

    old_score = job.get("overall_score")
    job["status"] = "approving"
    config = get_team_config(team_id)

    # Defense-in-depth: re-validate the analyst's payload against team
    # config so a client can't send yn_value="NA" on a section declared
    # na_applicable=false. The frontend already gates the dropdown, but
    # we don't trust it on the server side.
    sections_by_id = {s.id: s for s in config.sections}
    try:
        approval = ApprovalRequest.model_validate(
            approval.model_dump(), context={"sections_by_id": sections_by_id}
        )
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Approval payload invalid: {e}")

    # Post-flip: finalized evaluations are immutable from the UI — except
    # through the S7 edit-of-finalized doorway, whose caller enforced the
    # §4.3a acknowledgment before setting the flag.
    if job.get("state") == "finalized" and not edit_of_finalized:
        job["status"] = "complete"
        raise HTTPException(status_code=409, detail="Evaluation is finalized (engine-scored) — read-only")

    # Engine approval (CutoverDesign §2/§5): DB transition + engine compute
    # + projections. No destination tab, no readback — DB errors are hard
    # errors (§7.3 Phase C).
    sc = job["scorecard"]
    # Payload evaluator wins (S1: restored jobs have no original manager);
    # then the job's manager; then the row's evaluator already inside
    # manager_email for restored jobs. All empty → 422, because "" would
    # satisfy the NOT-NULL approval CHECK while recording nobody.
    evaluator_email = approval.evaluator_email or sc.get("manager_email", "")
    if not evaluator_email:
        job["status"] = "complete"
        raise HTTPException(
            status_code=422,
            detail="evaluator_email required: approval context has no evaluator identity",
        )
    # Backend guard behind the frontend's "Score Required" gate: manual
    # sections without a formula na_default must carry real scores —
    # NA would ride into full credit (the 2026-07-06 Sales find).
    unscored = eval_store.missing_manual_scores(config, approval.sections)
    if unscored:
        job["status"] = "complete"
        raise HTTPException(
            status_code=422,
            detail=f"Manual sections require scores before approval: {unscored}",
        )
    # §4.3a (S6): resolving a flagged review is a manual mutation of an
    # agent-visible score — the acknowledgment gate applies.
    resolving = job.get("scoring_status") == "flagged_human_review"
    if resolving and approval.acknowledged is not True:
        job["status"] = "complete"
        raise HTTPException(
            status_code=422,
            detail="acknowledged required: resolving a human-review flag "
                   "binds the evaluator to notify the agent that their "
                   "progression was manually modified (§4.3a)",
        )
    try:
        evaluation_id = await record_approval(
            config,
            evaluation_id=job.get("evaluation_id"),
            dialpad_link=sc.get("dialpad_link"),
            evaluator_email=evaluator_email,
            approved_sections=approval.sections,
            draft_sections=sc.get("sections", []),
            key_strengths=approval.key_strengths,
            opportunities=approval.opportunities,
            overall_score_raw=None,  # the engine produces the number
            agent_email=job.get("agent_email") or None,
            model=sc.get("model", "gemini-2.5-flash"),
            strict=True,
            resolving_review=resolving,
        )
        if evaluation_id is None:
            raise HTTPException(status_code=500, detail="No draft evaluation row to approve")
        detail = await eval_store.stamp_and_finalize(
            evaluation_id, config, evaluator_email=evaluator_email
        )
        # S7 (§4.1): a re-approved finalized score is a mid-chain change —
        # every later EWMA/SPC point is stale until the series rebuilds.
        # Loud on failure (500 + retryable), never silent.
        if edit_of_finalized and rebuild_agent_id is not None:
            pool = await eval_store.get_pool()
            if pool is not None:
                async with pool.acquire() as conn:
                    async with conn.transaction():
                        await rebuild_agent_series(conn, rebuild_agent_id, config)
        # S5 (§4.2): the approve path is a finalize seam too. On a fire,
        # suppress projection/SSE/email for this pass and schedule the
        # machine's retry after the response is sent.
        followup = await _maybe_auto_rescore(
            job, evaluation_id, float(detail.overall_score), config, team_id,
            key=key, api_key_role=identity.role,
            semaphore=_semaphore_for_key(identity),
            manager_email=evaluator_email,
        )
        # §4.3a.3 (S6): the resolution creates the coaching receipt —
        # a human looked, and the agent is owed notice. Created even
        # when the auto-rescore fired (the human DID resolve; the
        # suppressed email leaves the receipt pending, correctly).
        coaching_id = None
        receipt_summary = None
        if resolving:
            coaching_id = await eval_store.create_resolution_receipt(
                evaluation_id=evaluation_id, team_id=team_id,
                evaluator_email=evaluator_email,
                agent_name=job.get("agent_name") or sc.get("agent_name") or "the agent",
            )
            receipt_summary = "Agent notified via review-resolution email"
        elif edit_of_finalized:
            coaching_id = await eval_store.create_edit_receipt(
                evaluation_id=evaluation_id, team_id=team_id,
                evaluator_email=evaluator_email,
                agent_name=job.get("agent_name") or sc.get("agent_name") or "the agent",
                old_score=old_score,
            )
            receipt_summary = "Agent notified via edit-of-finalized disclaimer email"
        history_row = None
        if followup is None:
            pool = await eval_store.get_pool()
            history_row = await project_evaluation(
                pool, evaluation_id, config, include_history=True
            )
            await _dispatch_finalize_email(
                job, history_row, team_id, evaluation_id,
                default_disclaimer=(
                    "review_resolution" if resolving
                    else "edit_finalized" if edit_of_finalized else None
                ),
                suppress=suppress_email,
            )
            await _publish_finalized_event(
                team_id, job,
                agent_name=job.get("agent_name") or sc.get("agent_name"),
                evaluator_email=evaluator_email,
                overall_score=float(detail.overall_score),
                history_row=history_row,
                dialpad_link=sc.get("dialpad_link"),
                call_summary=sc.get("call_summary", ""),
                key_strengths=approval.key_strengths,
                opportunities=approval.opportunities,
            )
            # The disclaimer email discharges the §4.3a notification duty.
            dispatched_ok = (job.get("email_dispatch") or {}).get("status") \
                not in ("error", "suppressed", "skipped")
            if coaching_id is not None and dispatched_ok:
                await eval_store.complete_coaching_notified(
                    coaching_id, completed_by=evaluator_email,
                    summary=receipt_summary,
                )
        approve_notes = "engine-scored"
        if resolving:
            approve_notes += (
                f"; resolved_review ack:{evaluator_email}"
                f"; coaching={coaching_id if coaching_id is not None else 'none'}"
            )
        elif edit_of_finalized:
            approve_notes += (
                f"; edit_of_finalized {old_score} → {float(detail.overall_score)}"
                f" ack:{evaluator_email}"
                f"; coaching={coaching_id if coaching_id is not None else 'none'}"
            )
        await _append_audit_row_threaded(
            api_key_role=identity.role,
            evaluator_email=evaluator_email,
            agent_email=job.get("agent_email", ""),
            agent_name=job.get("agent_name") or sc.get("agent_name") or "",
            call_id=job.get("call_id", ""),
            target_team=team_id,
            action=audit_cfg.ACTION_APPROVED,
            result_row=history_row,
            notes=approve_notes,
        )
        job["status"] = "approved"
        job["state"] = "finalized"
        job["scoring_status"] = "complete"
        job["overall_score"] = float(detail.overall_score)
        job["evaluation_id"] = evaluation_id
        if followup is not None:
            background_tasks.add_task(_await_coro, followup)
        return {
            "status": "approved",
            "state": "finalized",
            "overall_score": float(detail.overall_score),
            "history_row": history_row,
            **({"auto_rescore": "scheduled"} if followup is not None else {}),
            **({
                "edited_finalized": True,
                "old_score": old_score,
                "coaching_id": coaching_id,
            } if edit_of_finalized else {}),
        }
    except HTTPException:
        job["status"] = "complete"
        raise
    except Exception as e:
        # Roll back to 'complete' so the manager can retry — the failed
        # DB transaction rolled back too, so the row is still consistent.
        job["status"] = "complete"
        job["error"] = str(e)
        raise HTTPException(status_code=500, detail=f"Engine approval failed: {e}")


async def _rescore_core(*, key, team_id, config, call_id, agent_name,
                        manager_email, duration_ms, expected_row_id, agent_id,
                        semaphore, api_key_role, audio_bytes, transcript_data,
                        call_details):
    """The §4.2 rescore primitive body, shared by the manual route and
    the S5 auto trigger: fresh score_call pass → Stage-1 upsert (REPLACE,
    same row id, loud guard) → series rebuild while the row is draft →
    normal auto-flow. Owns the job's status transitions, including the
    error state — callers just await it."""
    try:
        _jobs[key]["status"] = "scoring"
        async with semaphore:
            scorecard = await score_call(
                audio_bytes=audio_bytes,
                filename=f"{call_id}.mp3",
                call_id=call_id,
                agent_name=agent_name,
                manager_email=manager_email,
                config=config,
                duration_ms=duration_ms,
                transcript_data=transcript_data,
                call_details=call_details,
            )
        # §4.2.2: the Stage-1 upsert IS the REPLACE — same row id,
        # lifecycle columns reset, sections replaced wholesale.
        evaluation_id = await record_draft_evaluation(
            scorecard, config, strict=True
        )
        if evaluation_id != expected_row_id:
            raise RuntimeError(
                f"rescore landed on row {evaluation_id}, expected "
                f"{expected_row_id} — REPLACE contract violated "
                "(dialpad_link drift?)"
            )
        _jobs[key]["evaluation_id"] = evaluation_id
        _jobs[key]["scorecard"] = scorecard.model_dump()
        # §4.2.3: row is draft now — drop its stat point and rebuild
        # the series. Fatal by contract (§5): a failure errors the
        # whole job loudly rather than leaving a corrupt EWMA chain.
        if agent_id is not None:
            pool = await eval_store.get_pool()
            if pool is None:
                raise RuntimeError("rescore: no DB pool for series rebuild")
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await rebuild_agent_series(conn, agent_id, config)
        # §4.2.4: the auto-flow decides auto-finalize vs. flagged,
        # like any first pass — the machine owns the outcome. A manual
        # rescore landing ≤ threshold with the latch unstamped hands
        # over to the machine's one auto retry (finalize seam is
        # universal); the latch bounds the recursion at depth one.
        followup = await _postgres_post_stage1(
            _jobs[key], evaluation_id, scorecard, config, team_id,
            key=key, api_key_role=api_key_role, semaphore=semaphore,
        )
        _jobs[key]["status"] = "complete"
        if followup is not None:
            await followup
    except Exception as e:
        _jobs[key]["status"] = "error"
        _jobs[key]["error"] = str(e)


@router.post("/score/{job_id}/rescore")
async def rescore_evaluation(
    request: Request,
    job_id: str,
    payload: RescoreRequest,
    background_tasks: BackgroundTasks,
    identity: KeyIdentity = Depends(require_api_key),
):
    """Manual re-score — REPLACE on the same row id (ScorecardActionsDesign
    §4.2). Team key (roster-scoped) or privileged; scorecard and datapoint
    surfaces (job_id or bare call_id — §3 resolution).

    The rescore primitive: re-download the audio by Dialpad call id, run
    the same score_call pipeline (fresh model pass — including whatever
    SCORING_PIPELINE stage-A mode is live), land it via the Stage-1 upsert
    (same row id, every FK preserved), rebuild the agent's stat series so
    the superseded score doesn't linger in the EWMA chain while the row
    sits in draft, then let the normal auto-flow decide auto-finalize vs.
    flagged review. The re-finalize dispatches the disclaimer email by
    default; ``suppress_email`` opts out (§4.2.7).

    Machine-owned outcome: the manual act here is the *trigger*, not the
    score — no coaching receipt, no §4.3a acknowledgment (§0.2). The
    audit row is action='rescored' with the superseded score AND model
    stamp (``superseded_model=…``), so cross-model re-scores stay
    traceable once the judge model can differ between passes
    (TwoStageScoringDesign P3+).

    Concurrency: last-writer-wins by design. qa.evaluations has no
    updated_at column and these are low-traffic manager tools. If Landing
    outgrows this (concurrent evaluators on one eval), add updated_at +
    an If-Unmodified-Since-style optimistic check here — potential design
    choice deliberately deferred, not overlooked.
    """
    team_id = team_id_from_path(request)
    config = get_team_config(team_id)
    key = _job_key(team_id, job_id)

    # §4.2: refuse to race an in-flight job for the same key.
    existing = _jobs.get(key)
    if existing and existing.get("status") in {"pending", "scoring"}:
        raise HTTPException(
            status_code=409,
            detail=f"A scoring job for this call is already {existing['status']}",
        )

    ref = await eval_store.resolve_evaluation(team_id, job_id)
    if ref is None:
        raise HTTPException(status_code=404, detail="No evaluation matches this call id")

    # Auth (§7): roster-scoped for team keys, mirroring /score.
    is_in_roster = (
        await email_in_team_mails(ref.agent_email, team_id)
        if ref.agent_email else False
    )
    try:
        check_scoring_access(
            identity, team_id, ref.agent_email, is_in_roster=is_in_roster,
        )
    except HTTPException as exc:
        await _append_audit_row_threaded(
            api_key_role=identity.role,
            evaluator_email=payload.evaluator_email,
            agent_email=ref.agent_email or "",
            agent_name=ref.agent_name_raw,
            call_id=_ref_call_id(ref),
            target_team=team_id,
            action=audit_cfg.ACTION_DENIED,
            result_row=None,
            notes=f"http_{exc.status_code}",
        )
        raise

    # §4.2.1: same input, fresh pass — re-download by Dialpad call id.
    call_id = _ref_call_id(ref)
    try:
        audio_bytes = await download_recording(call_id)
    except NoRecordingAvailable:
        await _append_audit_row_threaded(
            api_key_role=identity.role,
            evaluator_email=payload.evaluator_email,
            agent_email=ref.agent_email or "",
            agent_name=ref.agent_name_raw,
            call_id=call_id,
            target_team=team_id,
            action=audit_cfg.ACTION_DENIED,
            result_row=None,
            notes="no_recording",
        )
        raise HTTPException(
            status_code=422,
            detail="Call has no recording in Dialpad",
        )
    except DialpadRateLimited:
        await _append_audit_row_threaded(
            api_key_role=identity.role,
            evaluator_email=payload.evaluator_email,
            agent_email=ref.agent_email or "",
            agent_name=ref.agent_name_raw,
            call_id=call_id,
            target_team=team_id,
            action=audit_cfg.ACTION_DENIED,
            result_row=None,
            notes="rate_limited",
        )
        raise HTTPException(
            status_code=503,
            detail="Dialpad rate-limited; retry shortly",
        )

    # Same pre-fetch pattern as /score (sequential in the handler so
    # fan-out background tasks don't burst Dialpad).
    transcript_data = await get_transcript(call_id)
    try:
        call_details = await get_call_details(call_id)
    except DialpadRateLimited:
        print(f"[rescore] Dialpad rate-limited fetching call_details for {call_id}; proceeding with blanks")
        call_details = None

    # Audit BEFORE scheduling (crash-safe receipt), with the superseded
    # score + model stamp — the cross-model traceability hook (P3+).
    old_score = "unscored" if ref.overall_score is None else ref.overall_score
    await _append_audit_row_threaded(
        api_key_role=identity.role,
        evaluator_email=payload.evaluator_email,
        agent_email=ref.agent_email or "",
        agent_name=ref.agent_name_raw,
        call_id=call_id,
        target_team=team_id,
        action=audit_cfg.ACTION_RESCORED,
        result_row=None,
        notes=f"manual: {old_score}; superseded_model={ref.model}",
    )

    _jobs[key] = {
        "status": "pending",
        "call_id": call_id,
        "agent_email": ref.agent_email or "",
        "agent_name": ref.agent_name_raw,
        "manager_email": payload.evaluator_email,
        # Consumed by _dispatch_finalize_email on the re-finalize
        # (in-memory only — see that helper's restart caveat).
        "rescore": {
            "cause": "manual",
            "suppress_email": payload.suppress_email,
            "superseded_score": ref.overall_score,
            "superseded_model": ref.model,
        },
    }

    semaphore = _semaphore_for_key(identity)
    agent_name = ref.agent_name_raw
    duration_ms = float(ref.duration_ms or 0)
    expected_row_id = ref.id
    agent_id = ref.agent_id

    async def run():
        await _rescore_core(
            key=key, team_id=team_id, config=config, call_id=call_id,
            agent_name=agent_name, manager_email=payload.evaluator_email,
            duration_ms=duration_ms, expected_row_id=expected_row_id,
            agent_id=agent_id, semaphore=semaphore,
            api_key_role=identity.role, audio_bytes=audio_bytes,
            transcript_data=transcript_data, call_details=call_details,
        )

    background_tasks.add_task(run)
    return {
        "job_id": job_id,
        "status": "pending",
        "evaluation_id": ref.id,
        "superseded_score": ref.overall_score,
    }


@router.post("/score/{job_id}/override")
async def override_scorecard(
    request: Request,
    job_id: str,
    payload: OverrideRequest,
    identity: KeyIdentity = Depends(require_api_key),
):
    """Override the auto score — SUPERSEDE riding source='ai_reviewed'
    (ScorecardActionsDesign §4.3). Team key (roster-scoped) or
    privileged. Scorecard surface only by design — an approval-flow
    action, not an archaeology action; the datapoint page never renders
    the control (S7 checkpoint).

    Target must be finalized. No precondition beyond that — the coaching
    record is not a gate you pass, it's a receipt the action writes
    (v1.2): one transaction sets the score a human stands behind, creates
    the pending qa.coachings receipt (+ 3-day notification deadline) and
    the coaching_evaluations link, resolves any standing review-queue
    entry, and rebuilds the stat series. Sections and formula_version
    stay — the superseded engine score is reproducible forever, so the
    UI can render "engine would say X" without storing X.

    §4.3a: `acknowledged` must be literally true (422 otherwise). The
    disclaimer email dispatches by default and mechanically discharges
    the notification duty (coaching completes); `suppress_email` leaves
    it pending — idx_coachings_pending is the accountability queue.

    Concurrency: last-writer-wins by design. qa.evaluations has no
    updated_at column and these are low-traffic manager tools. If Landing
    outgrows this (concurrent evaluators on one eval), add updated_at +
    an If-Unmodified-Since-style optimistic check here — potential design
    choice deliberately deferred, not overlooked.
    """
    team_id = team_id_from_path(request)
    config = get_team_config(team_id)
    key = _job_key(team_id, job_id)

    if payload.acknowledged is not True:
        raise HTTPException(
            status_code=422,
            detail="acknowledged required: overriding binds the evaluator "
                   "to notify the agent that their progression was manually "
                   "modified (§4.3a)",
        )

    existing = _jobs.get(key)
    if existing and existing.get("status") in {"pending", "scoring"}:
        raise HTTPException(
            status_code=409,
            detail=f"A scoring job for this call is already {existing['status']}",
        )

    ref = await eval_store.resolve_evaluation(team_id, job_id)
    if ref is None:
        raise HTTPException(status_code=404, detail="No evaluation matches this call id")

    is_in_roster = (
        await email_in_team_mails(ref.agent_email, team_id)
        if ref.agent_email else False
    )
    try:
        check_scoring_access(
            identity, team_id, ref.agent_email, is_in_roster=is_in_roster,
        )
    except HTTPException as exc:
        await _append_audit_row_threaded(
            api_key_role=identity.role,
            evaluator_email=payload.evaluator_email,
            agent_email=ref.agent_email or "",
            agent_name=ref.agent_name_raw,
            call_id=_ref_call_id(ref),
            target_team=team_id,
            action=audit_cfg.ACTION_DENIED,
            result_row=None,
            notes=f"http_{exc.status_code}",
        )
        raise

    if ref.state != "finalized":
        raise HTTPException(
            status_code=409,
            detail=f"Evaluation is '{ref.state}' — only finalized "
                   "evaluations can be overridden (draft evals go through "
                   "the normal edit + approve flow)",
        )
    if ref.agent_id is None:
        raise HTTPException(
            status_code=422,
            detail="Evaluation has no linked agent (qa.agents) — the "
                   "coaching receipt cannot be created. Import the agent "
                   "first (scripts/import_agents.py).",
        )

    old_score = ref.overall_score
    coaching_id = await eval_store.apply_override(
        evaluation_id=ref.id, team_id=team_id, agent_id=ref.agent_id,
        agent_name=ref.agent_name_raw, old_score=old_score,
        new_score=payload.overall_score, reasoning=payload.reasoning,
        conducted_by_role=payload.conducted_by_role,
        evaluator_email=payload.evaluator_email, config=config,
    )

    # Post-transaction, visible-not-blocking: projection, audit, email.
    pool = await eval_store.get_pool()
    history_row = await project_evaluation(
        pool, ref.id, config, include_history=True
    )
    notes = f"{old_score} → {payload.overall_score}: {payload.reasoning}"
    if payload.sop_gap is not None:
        doc = (
            f" (doc {payload.sop_gap.document_id})"
            if payload.sop_gap.document_id is not None else ""
        )
        notes += f" | SOP gap: {payload.sop_gap.note}{doc}"
    notes += f" | ack:{payload.evaluator_email}"
    await _append_audit_row_threaded(
        api_key_role=identity.role,
        evaluator_email=payload.evaluator_email,
        agent_email=ref.agent_email or "",
        agent_name=ref.agent_name_raw,
        call_id=_ref_call_id(ref),
        target_team=team_id,
        action=audit_cfg.ACTION_OVERRIDDEN,
        result_row=history_row,
        notes=notes,
    )

    coaching_status = "pending"
    email_dispatch: dict = {"status": "suppressed", "message": "suppress_email requested"}
    if not payload.suppress_email:
        try:
            email_dispatch = await asyncio.to_thread(
                trigger_apps_script, history_row, team_id, disclaimer="override"
            ) if history_row and history_row > 0 else {
                "status": "skipped", "message": "no Analyst_History row projected",
            }
        except Exception as e:
            email_dispatch = {"status": "error", "message": str(e)}
            logging.getLogger(__name__).exception(
                "override: disclaimer dispatch failed for eval %s (retryable; "
                "coaching stays pending)", ref.id,
            )
        if email_dispatch.get("status") not in ("error", "suppressed", "skipped"):
            # §4.3.5: the disclaimer email discharges the notification
            # duty mechanically — the receipt completes.
            await eval_store.complete_coaching_notified(
                coaching_id, completed_by=payload.evaluator_email,
                summary="Agent notified via override disclaimer email",
            )
            coaching_status = "completed"

    # Refresh the cached job so the scorecard page shows the new score.
    cached = _jobs.get(key)
    if cached:
        cached["overall_score"] = payload.overall_score
        cached["source"] = "ai_reviewed"

    return {
        "status": "overridden",
        "evaluation_id": ref.id,
        "old_score": old_score,
        "new_score": payload.overall_score,
        "superseded_engine_score": old_score,
        "coaching_id": coaching_id,
        "coaching_status": coaching_status,
        "history_row": history_row,
        "email_dispatch": email_dispatch,
    }


@router.post("/datapoints/{call_id}/edit")
async def edit_datapoint(
    request: Request,
    call_id: str,
    approval: ApprovalRequest,
    background_tasks: BackgroundTasks,
    identity: KeyIdentity = Depends(require_api_key),
):
    """The mirrored edit surface (ScorecardActionsDesign §4.1, S7) —
    keyed by Dialpad call id (or a full job id; §3 resolution accepts
    both), so it survives restarts and reaches years-old evaluations.

    Draft target → identical semantics to the scorecard approve (same
    payload, same validation, flagged-review resolutions included with
    their §4.3a gate). FINALIZED target → a re-approval: requires
    `acknowledged` (422 otherwise), re-runs the engine (which clears any
    standing S6 override — overall_score is recomputed from sections),
    re-projects, rebuilds the stat series, books the §4.3a edit coaching
    receipt, and re-sends with the edit_finalized disclaimer
    (`suppress_email` opts out; a successful dispatch discharges the
    receipt).

    Concurrency: last-writer-wins by design. qa.evaluations has no
    updated_at column and these are low-traffic manager tools. If Landing
    outgrows this (concurrent evaluators on one eval), add updated_at +
    an If-Unmodified-Since-style optimistic check here — potential design
    choice deliberately deferred, not overlooked.
    """
    team_id = team_id_from_path(request)
    key = _job_key(team_id, call_id)

    ref = await eval_store.resolve_evaluation(team_id, call_id)
    if ref is None:
        raise HTTPException(status_code=404, detail="No evaluation matches this call id")

    # Roster-scoped team keys or privileged (§7), mirroring rescore/override.
    is_in_roster = (
        await email_in_team_mails(ref.agent_email, team_id)
        if ref.agent_email else False
    )
    try:
        check_scoring_access(
            identity, team_id, ref.agent_email, is_in_roster=is_in_roster,
        )
    except HTTPException as exc:
        await _append_audit_row_threaded(
            api_key_role=identity.role,
            evaluator_email=approval.evaluator_email or "",
            agent_email=ref.agent_email or "",
            agent_name=ref.agent_name_raw,
            call_id=_ref_call_id(ref),
            target_team=team_id,
            action=audit_cfg.ACTION_DENIED,
            result_row=None,
            notes=f"http_{exc.status_code}",
        )
        raise

    # Cross-surface race guard: the scorecard keys jobs as
    # <call_id>_<agent>, this surface as the bare call id — check by
    # payload, not key.
    in_flight = _call_in_flight(
        team_id,
        {c for c in (ref.dialpad_call_id, ref.dialpad_entry_point_call_id) if c},
    )
    if in_flight:
        raise HTTPException(
            status_code=409,
            detail=f"A scoring job for this call is already {in_flight}",
        )

    edit_of_finalized = ref.state == "finalized"
    if edit_of_finalized:
        if approval.acknowledged is not True:
            raise HTTPException(
                status_code=422,
                detail="acknowledged required: editing a finalized "
                       "evaluation binds the evaluator to notify the agent "
                       "that their progression was manually modified (§4.3a)",
            )
        if ref.agent_id is None:
            raise HTTPException(
                status_code=422,
                detail="Evaluation has no linked agent (qa.agents) — the "
                       "coaching receipt cannot be created. Import the "
                       "agent first (scripts/import_agents.py).",
            )

    # Always rebuild the context from DB truth — this surface's whole
    # point is not trusting process memory.
    job = _job_from_ref(ref)
    _jobs[key] = job
    return await _approve_core(
        team_id=team_id, key=key, job=job, approval=approval,
        identity=identity, background_tasks=background_tasks,
        edit_of_finalized=edit_of_finalized,
        rebuild_agent_id=ref.agent_id if edit_of_finalized else None,
        suppress_email=approval.suppress_email if edit_of_finalized else False,
    )


@router.get("/review-queue")
async def review_queue(
    request: Request,
    identity: KeyIdentity = Depends(require_api_key),
):
    """The §0.3 review queue — every evaluation that WOULD require a
    human look and hasn't had one (human_review_required_at set,
    human_review_completed_at not). This is the accounting surface for
    the no-hard-blockers doctrine: blocked drafts (authoritative mode),
    shipped-but-queued rows (informative mode), and still-low
    auto-rescore survivors all appear here until a human resolution
    (approve) or an override stamps them completed."""
    team_id = team_id_from_path(request)
    rows = await eval_store.list_review_queue(team_id)
    return {"team_id": team_id, "count": len(rows), "evaluations": rows}


@router.delete("/score/{job_id}")
async def delete_evaluation(
    request: Request,
    job_id: str,
    identity: KeyIdentity = Depends(require_api_key),
):
    """Delete an evaluation — the privileged-only tampering doorway
    (ScorecardActionsDesign §4.4; formalizes the 2026-07-24 manual
    procedure).

    Order inside one transaction: stat point out (its FK is NO ACTION and
    would block the row delete), evaluation out (sections/tags/sweeps
    cascade), agent series rebuilt (§5 — the manual procedure got lucky
    with a latest-point delete; this route must not depend on luck).
    Coaching links refuse the delete with a 409 — they are the human-side
    tampering ledger and cascading them would erase the receipts that
    justify score changes.

    The response carries a full JSON snapshot of what was deleted (row +
    sections + stat point) — the caller's client-side backup, replacing
    the ad-hoc eval_2377_backup.json.

    Concurrency: last-writer-wins by design. qa.evaluations has no
    updated_at column and these are low-traffic manager tools. If Landing
    outgrows this (concurrent evaluators on one eval), add updated_at +
    an If-Unmodified-Since-style optimistic check here — potential design
    choice deliberately deferred, not overlooked.
    """
    team_id = team_id_from_path(request)
    if identity.role != "privileged":
        await _append_audit_row_threaded(
            api_key_role=identity.role,
            evaluator_email="",
            agent_email="",
            agent_name="",
            call_id=eval_store.call_id_from_job_id(job_id),
            target_team=team_id,
            action=audit_cfg.ACTION_DENIED,
            result_row=None,
            notes="delete_requires_privileged_key",
        )
        raise HTTPException(
            status_code=403,
            detail="Deleting evaluations requires the privileged API key",
        )

    ref = await eval_store.resolve_evaluation(team_id, job_id)
    if ref is None:
        raise HTTPException(status_code=404, detail="No evaluation matches this call id")
    config = get_team_config(team_id)

    pool = await eval_store.get_pool()
    if pool is None:
        raise HTTPException(status_code=503, detail="Database not configured")

    async with pool.acquire() as conn:
        coaching_rows = await conn.fetch(
            "SELECT coaching_id, linked_at FROM qa.coaching_evaluations "
            "WHERE evaluation_id = $1", ref.id,
        )
        if coaching_rows:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "Evaluation has coaching/1:1 records and cannot be "
                               "deleted — they are the audit trail for score "
                               "changes. Unlink the coachings first if this "
                               "delete is truly intended.",
                    "coaching_ids": [r["coaching_id"] for r in coaching_rows],
                },
            )

        # Snapshot BEFORE deleting — the response is the caller's backup,
        # and dialpad_link must outlive the row for the tombstone.
        eval_row = await conn.fetchrow(
            "SELECT * FROM qa.evaluations WHERE id = $1", ref.id)
        section_rows = await conn.fetch(
            "SELECT * FROM qa.evaluation_sections WHERE evaluation_id = $1 "
            "ORDER BY section_number", ref.id)
        point_row = await conn.fetchrow(
            "SELECT * FROM qa.agent_stat_points WHERE evaluation_id = $1", ref.id)
        snapshot = {
            "evaluation": dict(eval_row) if eval_row else None,
            "sections": [dict(r) for r in section_rows],
            "stat_point": dict(point_row) if point_row else None,
        }
        agent_id = eval_row["agent_id"] if eval_row else None

        async with conn.transaction():
            await conn.execute(
                "DELETE FROM qa.agent_stat_points WHERE evaluation_id = $1", ref.id)
            await conn.execute(
                "DELETE FROM qa.evaluations WHERE id = $1", ref.id)
            if agent_id is not None:
                # Fatal by contract — a failed rebuild rolls the delete back.
                await rebuild_agent_series(conn, agent_id, config)

    # Post-commit, both retryable/visible rather than delete-blocking:
    tombstone_row = await tombstone_evaluation(ref.dialpad_link or "", config)
    await _append_audit_row_threaded(
        api_key_role=identity.role,
        evaluator_email="",
        agent_email=ref.agent_email or "",
        agent_name=ref.agent_name_raw,
        call_id=_ref_call_id(ref),
        target_team=team_id,
        action=audit_cfg.ACTION_ORPHANED,
        result_row=tombstone_row,
        notes=f"role={identity.role} old_score={ref.overall_score}; "
              "backup returned in delete response",
    )
    # Evict any cached job so the scorecard page stops serving the ghost.
    _jobs.pop(_job_key(team_id, job_id), None)

    return {
        "status": "deleted",
        "evaluation_id": ref.id,
        "tombstoned_history_row": tombstone_row,
        "snapshot": snapshot,
    }

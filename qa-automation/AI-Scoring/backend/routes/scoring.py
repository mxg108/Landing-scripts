"""FastAPI routes for the QA scoring pipeline.

Registered twice in main.py:
  - /api/{team_id}/...  (team-aware, TEAM_AUTH_DEPENDENCY)
  - /api/...            (legacy shim, resolves team_id='member_support')
"""

from __future__ import annotations

import asyncio
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
from backend.models.scorecard import ApprovalRequest
from backend.services.history_service import (
    agent_email_for_name,
    agent_name_for_email,
    email_in_team_mails,
)
from backend.services.eval_store import record_approval, record_draft_evaluation
from backend.services.event_bus import get_event_bus
from backend.services.scoring_service import score_call
from backend.services.sheets_service import (
    append_score_audit_row,
    apply_analyst_edits_to_fr_ai,
    finalize_to_analyst_history,
    read_score_and_writeback,
    trigger_apps_script,
    write_draft_to_fr_ai,
    write_to_score_destination,
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
        append_score_audit_row(
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
            append_score_audit_row(
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
            append_score_audit_row(
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
    append_score_audit_row(
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
            row_num = write_draft_to_fr_ai(scorecard, config)
            # Stage 1 dual-write (Wave 2 Phase 4a) — §7.3 Phase A: never
            # raises; Postgres failures are logged and swallowed.
            evaluation_id = await record_draft_evaluation(scorecard, config)
            _jobs[key]["status"] = "complete"
            _jobs[key]["sheets_row"] = row_num
            _jobs[key]["evaluation_id"] = evaluation_id
            _jobs[key]["scorecard"] = scorecard.model_dump()
        except Exception as e:
            _jobs[key]["status"] = "error"
            _jobs[key]["error"] = str(e)

    background_tasks.add_task(run)
    return {"job_id": job_id, "status": "pending"}


@router.get("/score/{job_id}")
async def get_score_result(request: Request, job_id: str):
    """Poll for the result of a scoring job."""
    team_id = team_id_from_path(request)
    job = _jobs.get(_job_key(team_id, job_id))
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


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

        append_score_audit_row(
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
                row_num = write_draft_to_fr_ai(scorecard, config)
                evaluation_id = await record_draft_evaluation(scorecard, config)
                _jobs[k]["status"] = "complete"
                _jobs[k]["sheets_row"] = row_num
                _jobs[k]["evaluation_id"] = evaluation_id
                _jobs[k]["scorecard"] = scorecard.model_dump()
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
    identity: KeyIdentity = Depends(require_api_key),
):
    """
    Manager approves a scored call (optionally with edits).
    Updates Form Responses AI, copies to Form Responses 1,
    waits for ARRAYFORMULA, then triggers Apps Script email pipeline.

    Writes a Score_Audit row with action="approved" once Stage 4
    succeeds — captured before the Apps Script email dispatch so an
    Apps Script failure still leaves the audit trail intact.
    """
    team_id = team_id_from_path(request)
    key = _job_key(team_id, job_id)
    job = _jobs.get(key)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] == "approved":
        raise HTTPException(status_code=409, detail="Already approved")
    if job["status"] != "complete":
        raise HTTPException(
            status_code=409,
            detail=f"Job is '{job['status']}', must be 'complete' to approve",
        )

    sheets_row = job.get("sheets_row", -1)
    if sheets_row < 1:
        raise HTTPException(
            status_code=500,
            detail="No valid Sheets row recorded for this job",
        )

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

    try:
        sections_dicts = [s.model_dump() for s in approval.sections]
        sc = job["scorecard"]
        # Treat the scoring-time manager_email as the evaluator at approval
        # time. Future: replace with authenticated session user.
        evaluator_email = sc.get("manager_email", "")

        # Stage 1.5 — apply analyst edits (sections + feedback) to FR-AI
        print(f"[approve] Stage 1.5: applying analyst edits to FR-AI row {sheets_row}...")
        apply_analyst_edits_to_fr_ai(
            fr_ai_row_num=sheets_row,
            sections=sections_dicts,
            config=config,
            key_strengths=approval.key_strengths,
            opportunities=approval.opportunities,
        )
        print("[approve] Stage 1.5 complete.")

        # Stage 2 — write to per-team score destination tab
        print(f"[approve] Stage 2: writing to score destination ({config.sheets.score_destination.tab_name})...")
        dest_row = write_to_score_destination(
            fr_ai_row_num=sheets_row,
            config=config,
            evaluator_email=evaluator_email,
        )
        print(f"[approve] Stage 2 complete. Destination row: {dest_row}")

        # Stage 3 — poll readback col, write overall_score back to FR-AI col F
        print(f"[approve] Stage 3: polling {config.sheets.score_destination.score_readback_col}{dest_row} for ARRAYFORMULA result...")
        overall_score = await read_score_and_writeback(
            dest_row_num=dest_row,
            fr_ai_row_num=sheets_row,
            config=config,
        )
        print(f"[approve] Stage 3 complete. Overall score: '{overall_score}'")

        # Stage 4 — finalize to Analyst_History
        print(f"[approve] Stage 4: finalizing to Analyst_History...")
        history_row = finalize_to_analyst_history(
            fr_ai_row_num=sheets_row,
            evaluator_email=evaluator_email,
            config=config,
        )
        print(f"[approve] Stage 4 complete. Analyst_History row: {history_row}")

        # Postgres dual-write for the whole approve transition (Stages
        # 1.5+2+3+4 — Wave 2 Phase 4b). §7.3 Phase A: never raises.
        job["evaluation_id"] = await record_approval(
            config,
            evaluation_id=job.get("evaluation_id"),
            dialpad_link=sc.get("dialpad_link"),
            evaluator_email=evaluator_email,
            approved_sections=approval.sections,
            draft_sections=sc.get("sections", []),
            key_strengths=approval.key_strengths,
            opportunities=approval.opportunities,
            overall_score_raw=overall_score,
            agent_email=job.get("agent_email") or None,
            model=sc.get("model", "gemini-2.5-flash"),
        )

        # Audit the approval BEFORE Stage 5 so a downstream Apps Script
        # failure doesn't lose the record. Identity comes from the API key
        # used to hit /approve (may differ from the one that hit /score —
        # e.g. a privileged operator approving on behalf of a team).
        append_score_audit_row(
            api_key_role=identity.role,
            evaluator_email=evaluator_email,
            agent_email=job.get("agent_email", ""),
            agent_name=job.get("agent_name") or sc.get("agent_name") or "",
            call_id=job.get("call_id", ""),
            target_team=team_id,
            action=audit_cfg.ACTION_APPROVED,
            result_row=history_row,
            notes="",
        )

        # Publish "eval_approved" to subscribed dashboards. Fires AFTER
        # the audit row is written (no event for a half-finalized eval)
        # and BEFORE Apps Script dispatch (the toast races the email,
        # which is fine — the dashboard reflects approved state; the
        # email is the deliverable). Truncates free-text fields to 280
        # chars to bound per-client SSE payload bytes (LiveDashboard.md
        # resolved Q2). Full text remains available via /datapoint.
        def _truncate(s: str, n: int = 280) -> str:
            s = s or ""
            return s if len(s) <= n else s[: n - 1] + "…"

        # `eval_id` is the datapoint-route key (entry_point_call_id for
        # inbound queue calls, master call_id for direct calls). It comes
        # from the trailing path segment of `dialpad_link`, which
        # scoring_service.py already builds via build_dialpad_link with
        # the entry_point_call_id captured by get_call_details. Mirrors
        # the parse in services/team_stats.py:_parse_row so SSE drill-down
        # URLs match the Analyst_History eval_id used by /datapoint.
        def _eval_id_from_link(link: str) -> str:
            if not link:
                return ""
            clean = link.split("[")[0].strip().split("?")[0].strip()
            return clean.rstrip("/").split("/")[-1]

        dialpad_link = sc.get("dialpad_link", "")
        eval_id = _eval_id_from_link(dialpad_link) or job.get("call_id", "")

        await get_event_bus().publish(team_id, "eval_approved", {
            "call_id": job.get("call_id", ""),
            "eval_id": eval_id,
            "history_row": history_row,
            "agent": job.get("agent_name") or sc.get("agent_name") or "",
            "evaluator_email": evaluator_email,
            "overall_score": overall_score,
            "summary": _truncate(sc.get("call_summary", "")),
            "strengths": _truncate(approval.key_strengths),
            "opportunities": _truncate(approval.opportunities),
            "dialpad_link": dialpad_link,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

        # Stage 5 — dispatch QA email via Apps Script (reads AH row).
        print(f"[approve] Triggering Apps Script doPost (history_row={history_row})...")
        script_response = trigger_apps_script(history_row, config.team_id)
        print(f"[approve] Apps Script response: {script_response}")

        job["status"] = "approved"
        job["destination_row"] = dest_row
        job["history_row"] = history_row
        job["overall_score"] = overall_score

        return {
            "status": "approved",
            "fr_ai_row": sheets_row,
            "destination_row": dest_row,
            "history_row": history_row,
            "overall_score": overall_score,
            "script_response": script_response,
        }

    except Exception as e:
        job["status"] = "complete"  # roll back so manager can retry
        raise HTTPException(status_code=500, detail=f"Approval failed: {str(e)}")

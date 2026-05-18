"""FastAPI routes for the QA scoring pipeline.

Registered twice in main.py:
  - /api/{team_id}/...  (team-aware, TEAM_AUTH_DEPENDENCY)
  - /api/...            (legacy shim, resolves team_id='member_support')
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, UploadFile, File, Form

from backend.config.team_config import get_team_config
from backend.middleware.auth import team_id_from_path
from backend.models.scorecard import ApprovalRequest
from backend.services.scoring_service import score_call
from backend.services.sheets_service import (
    write_draft_to_fr_ai,
    apply_analyst_edits_to_fr_ai,
    write_to_score_destination,
    read_score_and_writeback,
    finalize_to_analyst_history,
    trigger_apps_script,
)
from backend.services.dialpad_client import (
    get_user_id_by_name,
    get_calls_for_agent,
    get_transcript,
    get_call_details,
    DialpadRateLimited,
)

router = APIRouter(tags=["scoring"])

# In-memory job store. Keyed by f"{team_id}:{job_id}" so jobs from one team
# cannot be read by another.
_jobs: dict[str, dict] = {}


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
    audio_file: UploadFile = File(...),
    call_id: str = Form(...),
    agent_name: str = Form(...),
    manager_email: str = Form(...),
    duration_ms: float = Form(default=0),
):
    """
    Score a single call.
    Accepts an audio file upload + form fields.
    Runs scoring in background and writes result to Google Sheets.
    Returns a job_id to poll for status.
    """
    team_id = team_id_from_path(request)
    audio_bytes = await audio_file.read()
    job_id = _make_job_id(call_id, agent_name)
    key = _job_key(team_id, job_id)
    _jobs[key] = {"status": "pending", "call_id": call_id}
    config = get_team_config(team_id)

    # Pre-fetch Dialpad metadata in the handler (sequential per request) so
    # fan-out background tasks don't burst Dialpad and lose metadata to 429s.
    transcript_data = await get_transcript(call_id)
    try:
        call_details = await get_call_details(call_id)
    except DialpadRateLimited:
        print(f"[score] Dialpad rate-limited fetching call_details for {call_id}; proceeding with blanks")
        call_details = None

    async def run():
        try:
            _jobs[key]["status"] = "scoring"
            scorecard = await score_call(
                audio_bytes=audio_bytes,
                filename=audio_file.filename,
                call_id=call_id,
                agent_name=agent_name,
                manager_email=manager_email,
                config=config,
                duration_ms=duration_ms,
                transcript_data=transcript_data,
                call_details=call_details,
            )
            row_num = write_draft_to_fr_ai(scorecard, config)
            _jobs[key]["status"] = "complete"
            _jobs[key]["sheets_row"] = row_num
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
    audio_files: list[UploadFile] = File(...),
    call_ids: str = Form(...),        # comma-separated, matching order of audio_files
    agent_name: str = Form(...),
    manager_email: str = Form(...),
    durations_ms: str = Form(default=""),  # comma-separated, matching order
):
    """
    Score multiple calls in one submission.
    audio_files and call_ids must be in the same order.
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
    job_ids = []
    for i, (audio_file, call_id) in enumerate(zip(audio_files, id_list)):
        audio_bytes = await audio_file.read()
        duration = dur_list[i] if i < len(dur_list) else 0
        job_id = _make_job_id(call_id, agent_name)
        key = _job_key(team_id, job_id)
        _jobs[key] = {"status": "pending", "call_id": call_id}
        job_ids.append(job_id)

        async def run(ab=audio_bytes, fn=audio_file.filename, cid=call_id, k=key, dur=duration):
            try:
                _jobs[k]["status"] = "scoring"
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
                _jobs[k]["status"] = "complete"
                _jobs[k]["sheets_row"] = row_num
                _jobs[k]["scorecard"] = scorecard.model_dump()
            except Exception as e:
                _jobs[k]["status"] = "error"
                _jobs[k]["error"] = str(e)

        background_tasks.add_task(run)

    return {"job_ids": job_ids, "count": len(job_ids)}


@router.post("/score/{job_id}/approve")
async def approve_scorecard(request: Request, job_id: str, approval: ApprovalRequest):
    """
    Manager approves a scored call (optionally with edits).
    Updates Form Responses AI, copies to Form Responses 1,
    waits for ARRAYFORMULA, then triggers Apps Script email pipeline.
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

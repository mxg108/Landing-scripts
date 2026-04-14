"""FastAPI routes for the QA scoring pipeline."""

from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, UploadFile, File, Form
from fastapi.responses import JSONResponse

from backend.config.team_config import get_team_config
from backend.models.scorecard import ApprovalRequest
from backend.services.scoring_service import score_call
from backend.services.sheets_service import (
    append_scorecard_row,
    update_scorecard_reasoning,
    write_approved_to_form_responses_1,
    trigger_apps_script,
)
from backend.services.dialpad_client import get_user_id_by_name, get_calls_for_agent
from datetime import datetime

router = APIRouter(prefix="/api", tags=["scoring"])

# In-memory job store (replace with DB in Phase 2)
_jobs: dict[str, dict] = {}


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
    audio_bytes = await audio_file.read()
    job_id = f"{call_id}_{agent_name}".replace(" ", "_")
    _jobs[job_id] = {"status": "pending", "call_id": call_id}
    config = get_team_config()

    async def run():
        try:
            _jobs[job_id]["status"] = "scoring"
            scorecard = await score_call(
                audio_bytes=audio_bytes,
                filename=audio_file.filename,
                call_id=call_id,
                agent_name=agent_name,
                manager_email=manager_email,
                config=config,
                duration_ms=duration_ms,
            )
            row_num = append_scorecard_row(scorecard, config)
            _jobs[job_id]["status"] = "complete"
            _jobs[job_id]["sheets_row"] = row_num
            _jobs[job_id]["scorecard"] = scorecard.model_dump()
        except Exception as e:
            _jobs[job_id]["status"] = "error"
            _jobs[job_id]["error"] = str(e)

    background_tasks.add_task(run)
    return {"job_id": job_id, "status": "pending"}


@router.get("/score/{job_id}")
async def get_score_result(job_id: str):
    """Poll for the result of a scoring job."""
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.post("/score/batch")
async def score_batch(
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
    id_list = [cid.strip() for cid in call_ids.split(",")]
    dur_list = [float(d.strip()) if d.strip() else 0 for d in durations_ms.split(",")] if durations_ms else []

    if len(audio_files) != len(id_list):
        raise HTTPException(
            status_code=400,
            detail=f"Mismatch: {len(audio_files)} files vs {len(id_list)} call IDs"
        )

    config = get_team_config()
    job_ids = []
    for i, (audio_file, call_id) in enumerate(zip(audio_files, id_list)):
        audio_bytes = await audio_file.read()
        duration = dur_list[i] if i < len(dur_list) else 0
        job_id = f"{call_id}_{agent_name}".replace(" ", "_")
        _jobs[job_id] = {"status": "pending", "call_id": call_id}
        job_ids.append(job_id)

        async def run(ab=audio_bytes, fn=audio_file.filename, cid=call_id, jid=job_id, dur=duration):
            try:
                _jobs[jid]["status"] = "scoring"
                scorecard = await score_call(
                    audio_bytes=ab,
                    filename=fn,
                    call_id=cid,
                    agent_name=agent_name,
                    manager_email=manager_email,
                    config=config,
                    duration_ms=dur,
                )
                row_num = append_scorecard_row(scorecard, config)
                _jobs[jid]["status"] = "complete"
                _jobs[jid]["sheets_row"] = row_num
                _jobs[jid]["scorecard"] = scorecard.model_dump()
            except Exception as e:
                _jobs[jid]["status"] = "error"
                _jobs[jid]["error"] = str(e)

        background_tasks.add_task(run)

    return {"job_ids": job_ids, "count": len(job_ids)}


@router.post("/score/{job_id}/approve")
async def approve_scorecard(job_id: str, approval: ApprovalRequest):
    """
    Manager approves a scored call (optionally with edits).
    Updates Form Responses AI, copies to Form Responses 1,
    waits for ARRAYFORMULA, then triggers Apps Script email pipeline.
    """
    job = _jobs.get(job_id)
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
    config = get_team_config()

    try:
        sections_dicts = [s.model_dump() for s in approval.sections]

        # 1. Update reasoning columns in Form AI (for enrichment),
        #    but preserve original AI scores as audit trail
        print(f"[approve] Step 1: updating reasoning in Form AI row {sheets_row}...")
        update_scorecard_reasoning(
            row_num=sheets_row,
            sections=sections_dicts,
            config=config,
            key_strengths=approval.key_strengths,
            opportunities=approval.opportunities,
        )
        print("[approve] Step 1 complete.")

        # 2. Write approved scores directly to Form Responses 1
        #    Metadata comes from the stored scorecard — no Sheets read needed
        sc = job["scorecard"]
        print(f"[approve] Step 2: writing approved row to Form Responses 1...")
        form_1_row = write_approved_to_form_responses_1(
            sections=sections_dicts,
            config=config,
            key_strengths=approval.key_strengths,
            opportunities=approval.opportunities,
            timestamp=datetime.now().strftime("%m/%d/%Y %H:%M:%S"),
            manager_email=sc.get("manager_email", ""),
            agent_name=sc.get("agent_name", ""),
            dialpad_link=sc.get("dialpad_link", ""),
        )
        print(f"[approve] Step 2 complete. Form 1 row: {form_1_row}")

        # 3. Wait for ARRAYFORMULA (col Q overall score, col V agent email)
        print("[approve] Step 3: waiting 2s for ARRAYFORMULA...")
        await asyncio.sleep(2)

        # 4. Trigger Apps Script email pipeline
        print(f"[approve] Step 4: triggering Apps Script doPost for row {form_1_row}...")
        script_response = trigger_apps_script(form_1_row)
        print(f"[approve] Step 4 complete: {script_response}")

        # 5. Mark as approved
        job["status"] = "approved"
        job["form_responses_1_row"] = form_1_row

        return {
            "status": "approved",
            "form_ai_row": sheets_row,
            "form_1_row": form_1_row,
            "script_response": script_response,
        }

    except Exception as e:
        job["status"] = "complete"  # roll back so manager can retry
        raise HTTPException(status_code=500, detail=f"Approval failed: {str(e)}")

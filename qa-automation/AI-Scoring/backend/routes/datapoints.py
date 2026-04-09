"""DataPoint API endpoints — evaluation drill-down."""

from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from backend.models.dashboard import EvaluationRecord
from backend.services.data_provider import get_provider

router = APIRouter(prefix="/api", tags=["datapoints"])


@router.get("/datapoints/{call_id}", response_model=EvaluationRecord)
async def get_datapoint(call_id: str):
    """Return a single evaluation record by call_id (extracted from Dialpad link)."""
    provider = await get_provider()
    all_records = await provider.get_all_history(days=365)

    for rec in all_records:
        if rec.eval_id == call_id:
            return rec

    raise HTTPException(404, f"No evaluation found for call_id '{call_id}'")


@router.get("/datapoints", response_model=list[EvaluationRecord])
async def list_datapoints(
    bin: Optional[str] = Query(default=None, description="Score range, e.g. '81-90'"),
    agent: Optional[str] = Query(default=None),
    days: int = Query(default=90, ge=1, le=365),
):
    """
    List evaluation records, filtered by score bin or agent name.
    At least one filter (bin or agent) is required.
    """
    if not bin and not agent:
        raise HTTPException(400, "Provide 'bin' (e.g. '81-90') or 'agent' query parameter")

    provider = await get_provider()

    if agent:
        records = await provider.get_agent_history(agent, days)
    else:
        records = await provider.get_all_history(days)

    if bin:
        try:
            lo, hi = bin.split("-")
            lo_val, hi_val = float(lo), float(hi)
        except ValueError:
            raise HTTPException(400, f"Invalid bin format '{bin}', expected 'lo-hi' (e.g. '81-90')")
        records = [r for r in records if lo_val <= r.overall_score <= hi_val]

    return records

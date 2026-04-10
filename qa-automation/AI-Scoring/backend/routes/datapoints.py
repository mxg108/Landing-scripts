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

    # Debug: find near-matches to diagnose why the lookup failed
    debug_matches = []
    for rec in all_records:
        link = rec.dialpad_link or ""
        if call_id in link or (rec.eval_id and call_id[:8] in rec.eval_id):
            debug_matches.append({
                "eval_id": rec.eval_id,
                "dialpad_link": link[:80],
                "agent": rec.agent_name,
                "manager": rec.manager_email,
            })

    detail = f"No evaluation found for call_id '{call_id}'."
    if debug_matches:
        detail += f" Found {len(debug_matches)} partial match(es): {debug_matches[:3]}"

    raise HTTPException(404, detail)


@router.get("/datapoints", response_model=list[EvaluationRecord])
async def list_datapoints(
    bin: Optional[str] = Query(default=None, description="Score range, e.g. '81-90'"),
    agent: Optional[str] = Query(default=None),
    days: int = Query(default=90, ge=1, le=365),
    active_only: bool = Query(default=True),
):
    """
    List evaluation records, filtered by score bin or agent name.
    At least one filter (bin or agent) is required.
    active_only filters to agents in the Mails roster (matches team dashboard).
    """
    if not bin and not agent:
        raise HTTPException(400, "Provide 'bin' (e.g. '81-90') or 'agent' query parameter")

    provider = await get_provider()

    if agent:
        records = await provider.get_agent_history(agent, days)
    else:
        records = await provider.get_all_history(days)

    # Filter to active agents only (matches team dashboard chart filters)
    if active_only and not agent:
        active_names = set()
        mails = provider._get_mails_sheet()
        for row in mails[1:]:
            if row and row[0].strip():
                active_names.add(row[0].strip().lower())
                # Also add canonical name if present
                if len(row) > 3 and row[3].strip():
                    active_names.add(row[3].strip().lower())
        records = [r for r in records if r.agent_name.strip().lower() in active_names]

    if bin:
        try:
            lo, hi = bin.split("-")
            lo_val, hi_val = float(lo), float(hi)
        except ValueError:
            raise HTTPException(400, f"Invalid bin format '{bin}', expected 'lo-hi' (e.g. '81-90')")
        records = [r for r in records if lo_val <= r.overall_score <= hi_val]

    return records

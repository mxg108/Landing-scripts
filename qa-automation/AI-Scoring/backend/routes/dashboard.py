"""Dashboard API endpoints for agent progression."""

from fastapi import APIRouter, HTTPException, Query
from backend.models.dashboard import EvaluationRecord, ProgressionAssessment
from backend.services.data_provider import get_provider
from backend.services.progression_service import get_progression

router = APIRouter(prefix="/api", tags=["dashboard"])


@router.get("/agents", response_model=list[str])
async def list_agents():
    """Return all unique agent names from evaluation history."""
    provider = await get_provider()
    return await provider.list_agents()


@router.get("/agents/{name}/history", response_model=list[EvaluationRecord])
async def agent_history(name: str, days: int = Query(default=30, ge=1, le=365)):
    """Return evaluation records for an agent within a time window."""
    provider = await get_provider()
    records = await provider.get_agent_history(name, days)
    if not records:
        raise HTTPException(404, f"No evaluations found for '{name}' in the last {days} days")
    return records


@router.get("/agents/{name}/progression", response_model=ProgressionAssessment)
async def agent_progression(name: str, days: int = Query(default=30, ge=1, le=365)):
    """Return Gemini-generated progression assessment for an agent."""
    provider = await get_provider()
    return await get_progression(provider, name, days)

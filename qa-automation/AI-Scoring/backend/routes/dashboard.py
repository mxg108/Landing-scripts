"""Dashboard API endpoints for agent progression.

Registered twice in main.py: team-aware (/api/{team_id}/...) and
legacy (/api/...).  team_id is extracted from the path via
``team_id_from_path``; legacy routes default to ``member_support``.
"""

from fastapi import APIRouter, HTTPException, Query, Request
from backend.config.team_config import get_team_config
from backend.middleware.auth import team_id_from_path
from backend.models.dashboard import EvaluationRecord, ProgressionAssessment
from backend.services.data_provider import get_provider
from backend.services.progression_service import get_progression

router = APIRouter(tags=["dashboard"])


@router.get("/agents", response_model=list[str])
async def list_agents(request: Request):
    """Return all unique agent names from evaluation history."""
    team_id = team_id_from_path(request)
    provider = await get_provider(team_id)
    return await provider.list_agents()


@router.get("/agents/{name}/history", response_model=list[EvaluationRecord])
async def agent_history(request: Request, name: str, days: int = Query(default=30, ge=1, le=365)):
    """Return evaluation records for an agent within a time window."""
    team_id = team_id_from_path(request)
    provider = await get_provider(team_id)
    records = await provider.get_agent_history(name, days)
    if not records:
        raise HTTPException(404, f"No evaluations found for '{name}' in the last {days} days")
    return records


@router.get("/agents/{name}/progression", response_model=ProgressionAssessment)
async def agent_progression(request: Request, name: str, days: int = Query(default=30, ge=1, le=365)):
    """Return Gemini-generated progression assessment for an agent."""
    team_id = team_id_from_path(request)
    config = get_team_config(team_id)
    provider = await get_provider(team_id)
    return await get_progression(provider, name, days, config=config)

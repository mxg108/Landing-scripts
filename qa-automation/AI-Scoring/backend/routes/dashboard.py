"""Dashboard API endpoints for agent progression.

Registered twice in main.py: team-aware (/api/{team_id}/...) and
legacy (/api/...).  team_id is extracted from the path via
``team_id_from_path``; legacy routes default to ``member_support``.
"""

from datetime import date, datetime, time, timezone

from fastapi import APIRouter, HTTPException, Query, Request
from backend.config.team_config import get_team_config
from backend.middleware.auth import team_id_from_path
from backend.models.dashboard import EvaluationRecord, ProgressionAssessment
from backend.services.data_provider import get_provider
from backend.services.progression_service import get_progression

router = APIRouter(tags=["dashboard"])

# Mirrors the team-route cap (see backend/routes/team.py:_MAX_RANGE_DAYS).
_MAX_RANGE_DAYS = 730


def _resolve_window(
    date_from: str | None,
    date_to: str | None,
) -> tuple[datetime, datetime] | None:
    """Parse the custom-range params into a tz-aware UTC (from, to) tuple.

    Returns None when neither is set. Raises 422 on malformed, inverted, or
    > 730-day ranges. UTC-aware because agent_history records carry tz-aware
    UTC timestamps (see history_service comments).
    """
    if not date_from and not date_to:
        return None
    if not (date_from and date_to):
        raise HTTPException(422, "date_from and date_to must be provided together")
    try:
        d_from = date.fromisoformat(date_from)
        d_to = date.fromisoformat(date_to)
    except ValueError:
        raise HTTPException(422, "date_from / date_to must be ISO YYYY-MM-DD")
    if d_to < d_from:
        raise HTTPException(422, "date_to must be on or after date_from")
    if (d_to - d_from).days + 1 > _MAX_RANGE_DAYS:
        raise HTTPException(422, f"range exceeds {_MAX_RANGE_DAYS}-day cap")
    return (
        datetime.combine(d_from, time.min, tzinfo=timezone.utc),
        datetime.combine(d_to, time.max, tzinfo=timezone.utc),
    )


@router.get("/agents", response_model=list[str])
async def list_agents(request: Request):
    """Return all unique agent names from evaluation history."""
    team_id = team_id_from_path(request)
    provider = await get_provider(team_id)
    return await provider.list_agents()


@router.get("/agents/{name}/history", response_model=list[EvaluationRecord])
async def agent_history(
    request: Request,
    name: str,
    days: int = Query(default=30, ge=1, le=730),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
):
    """Return evaluation records for an agent within a time window.

    When date_from/date_to are both provided, they override `days`. The
    service is still fetched with a days-back cutoff (sized to reach
    date_from), then results are filtered to drop anything after date_to —
    keeps the provider API unchanged.
    """
    team_id = team_id_from_path(request)
    provider = await get_provider(team_id)
    window = _resolve_window(date_from, date_to)

    if window is not None:
        # Reach back far enough to cover date_from, then post-filter to
        # drop anything after date_to (covers bonus windows that end in
        # the past, e.g. "May 1 — May 15" viewed in June).
        fetch_days = max(1, (datetime.now(timezone.utc) - window[0]).days + 1)
        fetch_days = min(fetch_days, _MAX_RANGE_DAYS)
        records = await provider.get_agent_history(name, fetch_days)
        records = [r for r in records if window[0] <= r.timestamp <= window[1]]
        if not records:
            raise HTTPException(
                404, f"No evaluations found for '{name}' between {date_from} and {date_to}"
            )
        return records

    records = await provider.get_agent_history(name, days)
    if not records:
        raise HTTPException(404, f"No evaluations found for '{name}' in the last {days} days")
    return records


@router.get("/agents/{name}/progression", response_model=ProgressionAssessment)
async def agent_progression(request: Request, name: str, days: int = Query(default=30, ge=1, le=730)):
    """Return Gemini-generated progression assessment for an agent.

    Progression remains days-only — the Gemini prompt and cache key are
    keyed on a days window. Frontend skips refresh when a custom range is
    active and falls back to the last preset assessment.
    """
    team_id = team_id_from_path(request)
    config = get_team_config(team_id)
    provider = await get_provider(team_id)
    return await get_progression(provider, name, days, config=config)

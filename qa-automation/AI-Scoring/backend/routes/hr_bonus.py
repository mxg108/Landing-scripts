"""HR bonus sheet endpoint — the month payload the GAS renderer consumes.

references/HRBonusSheet.md §5. Mounted team-aware only (/api/{team_id}/
hr-bonus/{month}); the GAS suite calls it with that team's API key. A
team without an hr_export config block 404s — this is how Sales stays
dark until its HR-visible section subset is decided (spec §8).
"""

from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException, Request

from backend.config.team_config import get_team_config
from backend.middleware.auth import team_id_from_path
from backend.services.hr_bonus_service import fetch_month_payload

router = APIRouter(prefix="/hr-bonus", tags=["hr-bonus"])

_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


@router.get("/{month}")
async def hr_bonus_month(month: str, request: Request):
    team_id = team_id_from_path(request)
    try:
        config = get_team_config(team_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Unknown team '{team_id}'")
    if config.hr_export is None:
        raise HTTPException(
            status_code=404,
            detail=f"Team '{team_id}' has no HR export configured",
        )
    if not _MONTH_RE.match(month):
        raise HTTPException(status_code=422, detail="month must be YYYY-MM")
    payload = await fetch_month_payload(config, month)
    if payload is None:
        raise HTTPException(status_code=503, detail="No database configured")
    return payload

"""Team-level analytics API endpoints."""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Query

from backend.models.team_stats import (
    MailsEntry,
    TeamStatsResponse,
)
from backend.services.data_provider import get_provider
from backend.services.team_stats import (
    compute_agent_roster,
    compute_binary_stats,
    compute_distribution,
    compute_ewma,
    compute_monthly_spc,
    compute_outliers,
    compute_section_analysis,
    compute_supervisor_stats,
    load_and_clean,
)

router = APIRouter(prefix="/api/team", tags=["team"])


@router.get("/stats", response_model=TeamStatsResponse)
async def team_stats(
    days: int = Query(default=90, ge=0, le=730),
    active_only: bool = Query(default=True),
    supervisor: str = Query(default=""),
):
    """Return all team-level statistical computations in one response."""
    provider = await get_provider()

    raw_history = provider._ws.get_all_values()
    raw_mails = provider._get_mails_sheet()

    df = load_and_clean(raw_history, raw_mails)

    if df.empty:
        return TeamStatsResponse(
            kpis={"total_evals": 0, "avg_score": 0, "std_score": 0, "analyst_count": 0},
            roster=[],
            outliers=[],
            spc={"months": [], "ucl": 0, "lcl": 0, "center": 0},
            section_analysis={"team_means": {}, "team_stds": {}, "training_opportunities": []},
            binary_stats={
                "identity_validation": {"team_pct": 0, "agents": []},
                "customer_resolution": {"team_pct": 0, "agents": []},
            },
            supervisor_stats=[],
            ewma=[],
            distribution=[],
            filters_applied={"days": days, "active_only": active_only, "supervisor": supervisor},
        )

    # Apply time filter (days=0 means all time)
    if days > 0:
        cutoff = datetime.now() - timedelta(days=days)
        df = df[df["timestamp"] >= cutoff]

    # Apply active filter
    if active_only:
        df = df[df["is_active"]]

    # Apply supervisor filter
    if supervisor:
        df = df[df["supervisor"].str.lower() == supervisor.strip().lower()]

    # Compute KPIs
    kpis = {
        "total_evals": len(df),
        "avg_score": round(float(df["overall_score"].mean()), 1) if len(df) > 0 else 0,
        "std_score": round(float(df["overall_score"].std(ddof=1)), 1) if len(df) > 1 else 0,
        "analyst_count": df["agent"].nunique(),
    }

    return TeamStatsResponse(
        kpis=kpis,
        roster=compute_agent_roster(df),
        outliers=compute_outliers(df),
        spc=compute_monthly_spc(df),
        section_analysis=compute_section_analysis(df),
        binary_stats=compute_binary_stats(df),
        supervisor_stats=compute_supervisor_stats(df),
        ewma=compute_ewma(df),
        distribution=compute_distribution(df),
        filters_applied={"days": days, "active_only": active_only, "supervisor": supervisor},
    )


@router.get("/mails", response_model=list[MailsEntry])
async def team_mails():
    """Return active analyst roster from Mails sheet."""
    provider = await get_provider()
    raw_mails = provider._get_mails_sheet()

    entries = []
    for row in raw_mails[1:]:  # skip header
        if len(row) < 2 or not row[0].strip():
            continue
        entries.append(MailsEntry(
            agent_name=row[0].strip(),
            email=row[1].strip() if len(row) > 1 else "",
            supervisor=row[2].strip() if len(row) > 2 else None,
            canonical_name=row[3].strip() if len(row) > 3 and row[3].strip() else None,
        ))

    return entries

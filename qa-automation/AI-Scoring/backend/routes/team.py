"""Team-level analytics API endpoints."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Query, Request

from backend.config.team_config import get_team_config
from backend.middleware.auth import team_id_from_path
from backend.models.team_stats import (
    LongFormResponse,
    LongFormRow,
    MailsEntry,
    TeamStatsResponse,
)
from backend.services.data_provider import get_provider
from backend.services.team_stats import (
    compute_agent_roster,
    compute_binary_stats,
    compute_distribution,
    compute_ewma,
    compute_long_form,
    compute_monthly_spc,
    compute_outliers,
    compute_section_analysis,
    compute_supervisor_stats,
    load_and_clean,
)

router = APIRouter(prefix="/team", tags=["team"])


# Coverage regime is hardcoded until Local AI scores 100% of calls
# (~1-2 months out per current roadmap). When that lands, this becomes
# either an env-var lookup or a per-row metadata tag, and consumers will
# need to filter long_form by regime to keep statistics comparable.
_COVERAGE_REGIME = "manager_sample"


@router.get("/stats", response_model=TeamStatsResponse)
async def team_stats(
    request: Request,
    days: int = Query(default=90, ge=0, le=730),
    active_only: bool = Query(default=True),
    supervisor: str = Query(default=""),
):
    """Return all team-level statistical computations in one response."""
    team_id = team_id_from_path(request)
    config = get_team_config(team_id)
    provider = await get_provider(team_id)

    raw_history = provider._ws.get_all_values()
    raw_mails = provider._get_mails_sheet()

    df = load_and_clean(raw_history, raw_mails, config)
    filters = {"days": days, "active_only": active_only, "supervisor": supervisor}

    if df.empty:
        return TeamStatsResponse(
            team_id=team_id,
            rubric_version=config.rubric_version,
            generated_at=datetime.now(timezone.utc),
            coverage_regime=_COVERAGE_REGIME,
            kpis={"total_evals": 0, "avg_score": 0, "std_score": 0, "analyst_count": 0},
            roster=[],
            outliers=[],
            spc={"months": [], "ucl": 0, "lcl": 0, "center": 0},
            section_analysis={"team_means": {}, "team_stds": {}, "training_opportunities": []},
            binary_stats=[
                {"section_id": sid, "label": label, "team_pct": 0.0, "agents": []}
                for sid, label in config.yn_section_labels.items()
            ],
            supervisor_stats=[],
            ewma=[],
            distribution=[],
            filters_applied=filters,
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

    kpis = {
        "total_evals": len(df),
        "avg_score": round(float(df["overall_score"].mean()), 1) if len(df) > 0 else 0,
        "std_score": round(float(df["overall_score"].std(ddof=1)), 1) if len(df) > 1 else 0,
        "analyst_count": df["agent"].nunique(),
    }

    return TeamStatsResponse(
        team_id=team_id,
        rubric_version=config.rubric_version,
        generated_at=datetime.now(timezone.utc),
        coverage_regime=_COVERAGE_REGIME,
        kpis=kpis,
        roster=compute_agent_roster(
            df,
            config.numeric_history_ids,
            config.section_labels,
            config.yn_history_ids,
            config.stats,
        ),
        outliers=compute_outliers(df, config.stats),
        spc=compute_monthly_spc(df, config.stats),
        section_analysis=compute_section_analysis(
            df, config.numeric_history_ids, config.section_labels
        ),
        binary_stats=compute_binary_stats(df, config.yn_section_labels),
        supervisor_stats=compute_supervisor_stats(df),
        ewma=compute_ewma(df, config.stats),
        distribution=compute_distribution(df),
        filters_applied=filters,
    )


@router.get("/long_form", response_model=LongFormResponse)
async def team_long_form(
    request: Request,
    days: int = Query(default=90, ge=0, le=730),
    active_only: bool = Query(default=True),
    supervisor: str = Query(default=""),
):
    """Return the canonical long-form analytical shape.

    One row per (evaluation, section). This is what every downstream
    predictive / ML / cohort analysis should start from. Today's
    dashboards still use /stats (wide-form rollups); long form exists
    so future feature stores and forecasters can consume a stable
    contract without re-deriving from raw history.
    """
    team_id = team_id_from_path(request)
    config = get_team_config(team_id)
    provider = await get_provider(team_id)

    raw_history = provider._ws.get_all_values()
    raw_mails = provider._get_mails_sheet()

    df = load_and_clean(raw_history, raw_mails, config)
    filters = {"days": days, "active_only": active_only, "supervisor": supervisor}

    if not df.empty:
        if days > 0:
            cutoff = datetime.now() - timedelta(days=days)
            df = df[df["timestamp"] >= cutoff]
        if active_only:
            df = df[df["is_active"]]
        if supervisor:
            df = df[df["supervisor"].str.lower() == supervisor.strip().lower()]

    long_df = compute_long_form(df, config)

    rows = [
        LongFormRow(
            agent=r["agent"],
            eval_id=r["eval_id"],
            timestamp=r["timestamp"],
            supervisor=r.get("supervisor", "") or "",
            manager_email=r.get("manager_email", "") or "",
            section_id=r["section_id"],
            section_name=r["section_name"],
            score_type=r["score_type"],
            score=r["score"],
        )
        for r in long_df.to_dict(orient="records")
    ]

    return LongFormResponse(
        team_id=team_id,
        rubric_version=config.rubric_version,
        generated_at=datetime.now(timezone.utc),
        coverage_regime=_COVERAGE_REGIME,
        rows=rows,
        filters_applied=filters,
    )


@router.get("/mails", response_model=list[MailsEntry])
async def team_mails(request: Request):
    """Return active analyst roster from Mails sheet."""
    team_id = team_id_from_path(request)
    provider = await get_provider(team_id)
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

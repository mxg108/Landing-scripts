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
    MonthlyBlock,
    MonthlySummary,
    TeamEvalRow,
    TeamEvalsResponse,
    TeamStatsResponse,
)
from backend.services.data_provider import get_provider
from backend.services.team_stats import (
    _months_in_bucket_tz,
    compute_agent_roster,
    compute_binary_stats,
    compute_distribution,
    compute_ewma,
    compute_long_form,
    compute_monthly_spc,
    compute_monthly_summary,
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
            monthly=compute_monthly_summary(df),
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

    # Apply active filter
    if active_only:
        df = df[df["is_active"]]

    # Apply supervisor filter
    if supervisor:
        df = df[df["supervisor"].str.lower() == supervisor.strip().lower()]

    # Monthly chiclets honor active/supervisor but NOT the days filter —
    # they express their own (last/current calendar month) windows.
    # Compute before applying days so the current-month chiclet still
    # populates when the user is viewing days=30 on the last day of a
    # month and the cutoff drops half the month.
    monthly = compute_monthly_summary(df)

    if days > 0:
        cutoff = datetime.now() - timedelta(days=days)
        df = df[df["timestamp"] >= cutoff]

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
        monthly=monthly,
        roster=compute_agent_roster(df, config),
        outliers=compute_outliers(df, config.stats),
        spc=compute_monthly_spc(df, config.stats),
        section_analysis=compute_section_analysis(df, config),
        binary_stats=compute_binary_stats(df, config.yn_section_labels),
        supervisor_stats=compute_supervisor_stats(df),
        ewma=compute_ewma(df, config.stats),
        distribution=compute_distribution(df),
        filters_applied=filters,
    )


@router.get("/evals", response_model=TeamEvalsResponse)
async def team_month_evals(
    request: Request,
    year_month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    active_only: bool = Query(default=True),
    supervisor: str = Query(default=""),
):
    """Return every eval in `year_month` (YYYY-MM), respecting active/supervisor.

    Powers the month-chiclet drill-down. The `days` filter is intentionally
    not accepted here: the month string IS the time window.
    """
    team_id = team_id_from_path(request)
    config = get_team_config(team_id)
    provider = await get_provider(team_id)

    raw_history = provider._ws.get_all_values()
    raw_mails = provider._get_mails_sheet()

    df = load_and_clean(raw_history, raw_mails, config)
    filters = {"active_only": active_only, "supervisor": supervisor}

    if df.empty:
        return TeamEvalsResponse(
            team_id=team_id,
            year_month=year_month,
            rows=[],
            filters_applied=filters,
        )

    if active_only:
        df = df[df["is_active"]]
    if supervisor:
        df = df[df["supervisor"].str.lower() == supervisor.strip().lower()]

    # Use the same project-TZ bucketing as compute_monthly_summary so a
    # call placed late local-time on the last of the month doesn't get
    # filtered into the wrong month here. Caught during PR-3 smoke
    # testing: 8:33 PM PDT May 31 was showing in June's drill-down.
    months = _months_in_bucket_tz(df["timestamp"])
    df = df[months == year_month]

    # Sort newest first — analysts skim the top to find recent calls.
    df = df.sort_values("timestamp", ascending=False)

    def _to_utc(ts):
        """Ensure outgoing ISO timestamps carry an explicit UTC marker.

        Sheet timestamps are written by sheets_service with
        `datetime.now(timezone.utc).strftime(...)` — i.e. UTC clock
        values stored as naive strings (no TZ marker in the cell).
        parse_timestamp reads them back as tz-naive datetimes. Without
        an explicit tzinfo, pydantic serializes them as e.g.
        `"2026-05-26T14:30:00"`, which JS parses as the BROWSER's local
        time — pushing them hours into the future for non-UTC browsers
        and tripping the time-ago helper's "just now" fallback.
        Stamping them as UTC restores correct relative-time math
        regardless of where the dashboard is viewed.
        """
        if ts is None:
            return ts
        # Pandas Timestamp and datetime both have .tzinfo / .tz; treat
        # naive as UTC. Already-aware values pass through unchanged.
        try:
            if ts.tzinfo is None:
                return ts.replace(tzinfo=timezone.utc)
        except AttributeError:
            pass
        return ts

    rows = [
        TeamEvalRow(
            agent=str(r["agent"]),
            timestamp=_to_utc(r["timestamp"]),
            overall_score=float(r["overall_score"]),
            # Wide-form df doesn't carry dialpad_link directly — reconstruct
            # the canonical URL from eval_id (the [LONG CALL] suffix isn't
            # preserved through load_and_clean, which is fine for the
            # drill-down list).
            dialpad_link=(
                f"https://dialpad.com/callhistory/callreview/{r['eval_id']}"
                if r.get("eval_id") else ""
            ),
            eval_id=str(r.get("eval_id", "")),
            supervisor=str(r.get("supervisor", "")) or None,
            evaluator_email=str(r.get("manager_email", "")) or None,
        )
        for r in df.to_dict(orient="records")
    ]

    return TeamEvalsResponse(
        team_id=team_id,
        year_month=year_month,
        rows=rows,
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


@router.get("/sections")
async def team_sections(request: Request):
    """Return the team's section list in canonical (section_number) order.

    Powers data-driven frontend rendering — the approve UI uses this to
    splice manual-section inputs into the AI-scored card list, and the
    section-aware dashboards consume the same shape (Phase D).

    Sections with `auto_value` set are returned for completeness; UIs
    typically skip them (writer hardcodes the value, no analyst input).
    """
    team_id = team_id_from_path(request)
    config = get_team_config(team_id)
    return [
        {
            "id": s.id,
            "history_id": s.history_id,
            "name": s.name,
            "section_number": s.section_number,
            "score_type": s.score_type,
            "audio_dependent": s.audio_dependent,
            "na_applicable": s.na_applicable,
            "auto_value": s.auto_value,
        }
        for s in config.sections_by_number
    ]


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

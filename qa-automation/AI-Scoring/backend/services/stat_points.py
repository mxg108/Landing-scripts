"""qa.agent_stat_points — incremental EWMA + SPC per finalize.

SQLMigration §9.2 / CutoverDesign 4c / ReadPathFlip F1. One row per
finalized evaluation per agent; three consumers (CC Zone D sparklines,
outlier chiclets via spc_flags, PDF assessment pipeline) read the series
by (agent_id, id).

Math notes:
- EWMA is the clean recursive form: ``ewma = λ·score + (1−λ)·prev``,
  seeded with the first score. λ derives from the team's StatsConfig
  span (λ = 2/(span+1)) and is PINNED on every row so historical series
  stay reproducible when the default span changes (§9.2).
  This is deliberately NOT the same number as the dashboard's
  window-scoped ``compute_ewma`` (pandas ewm over the filtered window) —
  ReadPathFlip §3 W4 decided the two coexist.
- SPC running mean/σ are Welford-updated through the current point (what
  a sparkline wants to draw); ``spc_flags`` are judged against the PRIOR
  point's stats so an anomalous score can't dilute its own control
  limits. Flag vocabulary: ``above_ucl`` / ``below_lcl`` at
  ``spc_sigma_multiplier``·σ, only once σ is defined (≥ 2 prior points)
  and positive.
- Writes are idempotent via the UNIQUE(evaluation_id) constraint
  (ON CONFLICT DO NOTHING) — deterministic replays repair any gap.

The finalize-time write is NON-FATAL by contract: a stat point is
derived data the replay script can rebuild; an approve must never 500
because of it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# Hardcoded until the Local AI cutover (~July 2026) moves coverage per-row
# — see the predictive-analytics groundwork note on qa.agent_stat_points
# (migration 006) and routes/team.py's matching constant.
COVERAGE_REGIME = "manager_sample"


def lambda_from_span(span: int) -> float:
    """pandas ewm span → smoothing factor λ = 2/(span+1), rounded to the
    column's NUMERIC(4,3) precision so the pinned value equals the stored
    value exactly."""
    return round(2.0 / (span + 1), 3)


@dataclass
class StatPoint:
    ewma: float
    ewma_lambda: float
    spc_mean: float
    spc_sigma: Optional[float]
    spc_flags: list[str]


@dataclass
class PriorState:
    """The previous point's running state (all None/0 for a first eval)."""
    n: int = 0
    ewma: Optional[float] = None
    mean: Optional[float] = None
    sigma: Optional[float] = None


def compute_point(score: float, prior: PriorState, *, span: int,
                  sigma_multiplier: float) -> StatPoint:
    """One incremental point from the prior state + the new score."""
    lam = lambda_from_span(span)
    ewma = score if prior.ewma is None else lam * score + (1 - lam) * float(prior.ewma)

    # Flags against the PRIOR control limits (see module docstring).
    flags: list[str] = []
    if prior.sigma is not None and prior.sigma > 0:
        center, band = float(prior.mean), sigma_multiplier * float(prior.sigma)
        if score > center + band:
            flags.append("above_ucl")
        elif score < center - band:
            flags.append("below_lcl")

    # Welford update through the current point (sample σ, ddof=1).
    n = prior.n + 1
    if prior.n == 0:
        mean, sigma = score, None
    else:
        prev_mean = float(prior.mean)
        mean = prev_mean + (score - prev_mean) / n
        prev_m2 = (float(prior.sigma) ** 2) * (prior.n - 1) if prior.sigma is not None else 0.0
        m2 = prev_m2 + (score - prev_mean) * (score - mean)
        sigma = (m2 / (n - 1)) ** 0.5

    return StatPoint(
        ewma=round(ewma, 2),
        ewma_lambda=lam,
        spc_mean=round(mean, 2),
        spc_sigma=round(sigma, 3) if sigma is not None else None,
        spc_flags=flags,
    )


async def load_prior_state(conn, agent_id: int) -> PriorState:
    """Previous point's running state for *agent_id* (latest by id)."""
    row = await conn.fetchrow(
        "SELECT ewma, spc_mean, spc_sigma, "
        "  (SELECT COUNT(*) FROM qa.agent_stat_points WHERE agent_id = $1) AS n "
        "FROM qa.agent_stat_points WHERE agent_id = $1 "
        "ORDER BY id DESC LIMIT 1", agent_id)
    if row is None:
        return PriorState()
    return PriorState(n=row["n"], ewma=float(row["ewma"]),
                      mean=float(row["spc_mean"]),
                      sigma=float(row["spc_sigma"]) if row["spc_sigma"] is not None else None)


async def insert_point(conn, *, team_id: str, agent_id: int, evaluation_id: int,
                       score: float, point: StatPoint,
                       coverage_regime: str = COVERAGE_REGIME) -> bool:
    """Idempotent insert; returns True when a row was written."""
    result = await conn.execute(
        "INSERT INTO qa.agent_stat_points "
        "(team_id, agent_id, evaluation_id, score, ewma, ewma_lambda, "
        " spc_mean, spc_sigma, spc_flags, coverage_regime) "
        "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) "
        "ON CONFLICT (evaluation_id) DO NOTHING",
        team_id, agent_id, evaluation_id, score, point.ewma,
        point.ewma_lambda, point.spc_mean, point.spc_sigma,
        point.spc_flags, coverage_regime)
    return result.endswith("1")


def build_series(evals: list[dict], *, span: int,
                 sigma_multiplier: float) -> list[dict]:
    """Deterministic per-agent series from approved_at-ordered evals.

    Moved here from scripts/backfill_stat_points.py (ScorecardActionsDesign
    §5) so the F1 replay CLI and the action-time rebuild share one math
    core — parity by construction, not by test."""
    prior = PriorState()
    rows = []
    for ev in evals:
        score = float(ev["overall_score"])
        point = compute_point(score, prior, span=span,
                              sigma_multiplier=sigma_multiplier)
        rows.append({"evaluation_id": ev["id"], "score": score, "point": point})
        prior = PriorState(n=prior.n + 1, ewma=point.ewma,
                           mean=point.spc_mean, sigma=point.spc_sigma)
    return rows


async def rebuild_agent_series(conn, agent_id: int, config) -> int:
    """Rebuild one agent's whole stat series from finalized history.

    Called by the scorecard actions (rescore / override / delete —
    ScorecardActionsDesign §5) after they change or remove a finalized
    score: the EWMA/SPC chain is recursive, so any mid-chain change
    invalidates every later point. The series is deterministic; DELETE +
    re-INSERT inside the caller's transaction is correct and idempotent.

    FATAL by contract — the finalize-time `write_point_for_finalize` is
    non-fatal because a missed point is a gap replay repairs, but a
    rebuild runs INSIDE a mutating transaction and a half-applied action
    with a silently corrupt EWMA chain is worse than a loud rollback.
    Callers must NOT wrap this in try/except.

    Returns the number of points written (0 for an agent with no
    finalized history — their stale points still get deleted)."""
    evals = await conn.fetch(
        "SELECT id, team_id, overall_score FROM qa.evaluations "
        "WHERE agent_id = $1 AND state = 'finalized' "
        "ORDER BY approved_at, id", agent_id)
    await conn.execute(
        "DELETE FROM qa.agent_stat_points WHERE agent_id = $1", agent_id)
    series = build_series(
        evals, span=config.stats.ewma_span,
        sigma_multiplier=config.stats.spc_sigma_multiplier)
    for ev, row in zip(evals, series):
        await insert_point(
            conn, team_id=ev["team_id"], agent_id=agent_id,
            evaluation_id=row["evaluation_id"], score=row["score"],
            point=row["point"])
    return len(series)


async def write_point_for_finalize(conn, evaluation_id: int, config) -> None:
    """Finalize-seam entry: read the (just-finalized) evaluation, compute
    the incremental point, insert. NON-FATAL — logs and returns on any
    failure; the replay script rebuilds gaps deterministically. Skips
    evaluations whose agent identity is unresolved (agent_id NULL —
    departed agents have no sparkline)."""
    try:
        ev = await conn.fetchrow(
            "SELECT team_id, agent_id, overall_score FROM qa.evaluations "
            "WHERE id = $1 AND state = 'finalized'", evaluation_id)
        if ev is None or ev["agent_id"] is None or ev["overall_score"] is None:
            if ev is not None and ev["agent_id"] is None:
                logger.info("stat_points: eval %s has no agent_id — no point",
                            evaluation_id)
            return
        prior = await load_prior_state(conn, ev["agent_id"])
        point = compute_point(
            float(ev["overall_score"]), prior,
            span=config.stats.ewma_span,
            sigma_multiplier=config.stats.spc_sigma_multiplier)
        await insert_point(conn, team_id=ev["team_id"], agent_id=ev["agent_id"],
                           evaluation_id=evaluation_id,
                           score=float(ev["overall_score"]), point=point)
    except Exception:  # noqa: BLE001 — non-fatal by contract
        logger.exception("stat_points: point write failed for eval %s "
                         "(replay repairs)", evaluation_id)

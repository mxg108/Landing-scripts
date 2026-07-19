"""qa.assessments writer — R3 persistence (JulyR2R3 §2).

Persists every FRESH progression generation (placeholders never). The
Wave-2 write-ban on qa.assessments is lifted per the July rollout
directive; migration 011's immutability rule stands: rows are pure AI
output, append-only — the ONLY mutation is flipping the prior row's
``is_current`` when a successor lands in the same window.

Window semantics: ``is_current`` flips among rows sharing
(agent_id, team_id, time_range_days). A calendar-month window whose day
count equals a rolling window (June = 30 = the dashboard default) can
therefore trade the flag back and forth with it — harmless by design:
readers that need a SPECIFIC window (the one-pager) match on the exact
``range_start_at``/``range_end_at`` ordered by ``generated_at`` and
ignore ``is_current`` entirely; ``is_current`` serves "latest for this
window size" listings only.

``estimated_cost_usd`` stays NULL until the cost-dashboard work ships a
price table (LateStageDesign Tier 3) — the column is nullable for
exactly this.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Optional

from backend.services.eval_store import get_pool

if TYPE_CHECKING:
    from backend.config.team_config import TeamConfig
    from backend.models.dashboard import ProgressionAssessment

logger = logging.getLogger(__name__)

_VALID_TRENDS = {"improving", "stable", "declining"}


async def persist_assessment(
    result: "ProgressionAssessment",
    *,
    agent_name: str,
    config: "TeamConfig",
    range_start: Optional[datetime] = None,
    range_end: Optional[datetime] = None,
) -> Optional[int]:
    """Insert one assessment (+ section rows) and flip the predecessor's
    ``is_current``. Returns the new assessment id, or None when
    persistence was skipped (no DB, unknown agent, no rubric version).
    Raises on real DB failures — callers choose the posture
    (get_progression swallows+logs; the EOM path lets it propagate).

    ``range_start``/``range_end`` override the window stamps for
    calendar-month (EOM) runs; the default is the rolling window ending
    now, matching the dashboard's ``days=N`` semantics.
    """
    pool = await get_pool()
    if pool is None:
        logger.info("assessment_store: no DATABASE_URL — skip persist")
        return None

    now = datetime.now(timezone.utc)
    end = range_end or now
    start = range_start or (end - timedelta(days=result.time_range_days))

    async with pool.acquire() as conn:
        agent_id = await conn.fetchval(
            "SELECT id FROM qa.agents "
            "WHERE team_id = $1 AND active "
            "  AND (LOWER(name) = LOWER($2) OR LOWER(canonical_name) = LOWER($2)) "
            "ORDER BY id LIMIT 1",
            config.team_id, agent_name.strip(),
        )
        if agent_id is None:
            logger.info(
                "assessment_store: no active qa.agents match for %r (%s) — "
                "skip persist (departed/unknown agents keep ephemeral cards)",
                agent_name, config.team_id,
            )
            return None

        from backend.services.score_compute import (
            VersionNotArchivedError,
            get_active_versions,
        )
        try:
            versions = await get_active_versions(conn, config.team_id)
        except VersionNotArchivedError:
            logger.warning(
                "assessment_store: no active rubric/formula version for %s — "
                "skip persist (rubric_version is NOT NULL)", config.team_id,
            )
            return None

        models_used = json.dumps({"text": {
            "provider": "gemini", "model": config.gemini.progression_model,
        }})

        async with conn.transaction():
            assessment_id = await conn.fetchval(
                "INSERT INTO qa.assessments "
                "(agent_id, team_id, time_range_days, range_start_at, "
                " range_end_at, evaluations_included, overall_assessment, "
                " rubric_version, formula_version, models_used, is_current) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, TRUE) "
                "RETURNING id",
                agent_id, config.team_id, result.time_range_days,
                start, end, result.evaluation_count,
                result.overall_assessment,
                versions.rubric_version, versions.formula_version,
                models_used,
            )
            # The one permitted mutation (migration 011 Q4.a): retire the
            # predecessor(s) in this window. Content columns untouched.
            await conn.execute(
                "UPDATE qa.assessments SET is_current = FALSE "
                "WHERE agent_id = $1 AND team_id = $2 "
                "  AND time_range_days = $3 AND is_current AND id <> $4",
                agent_id, config.team_id, result.time_range_days,
                assessment_id,
            )
            for history_id, sa in result.section_assessments.items():
                sec = config.history_id_to_section.get(history_id)
                if sec is None:
                    logger.warning(
                        "assessment_store: unknown section key %r — row skipped",
                        history_id,
                    )
                    continue
                trend = (sa.trend or "").strip().lower()
                if trend not in _VALID_TRENDS:
                    logger.warning(
                        "assessment_store: invalid trend %r for %s — row skipped",
                        sa.trend, sec.id,
                    )
                    continue
                await conn.execute(
                    "INSERT INTO qa.assessment_sections "
                    "(assessment_id, section_id, section_name, section_number, "
                    " trend, summary, coaching_tip) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7)",
                    assessment_id, sec.id, sec.name, sec.section_number,
                    trend, sa.summary, sa.coaching_tip,
                )
    logger.info(
        "assessment_store: persisted assessment %s for %r (%s, %sd window, "
        "%s evals)", assessment_id, agent_name, config.team_id,
        result.time_range_days, result.evaluation_count,
    )
    return assessment_id


async def fetch_assessment_for_range(
    config: "TeamConfig",
    agent_name: str,
    range_start: datetime,
    range_end: datetime,
) -> Optional[dict]:
    """Latest persisted assessment matching this EXACT window (the
    one-pager read path — deliberately ignores ``is_current``; see module
    docstring). Returns the row + section rows as plain dicts, or None."""
    pool = await get_pool()
    if pool is None:
        return None
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT a.* FROM qa.assessments a "
            "JOIN qa.agents ag ON ag.id = a.agent_id "
            "WHERE a.team_id = $1 "
            "  AND (LOWER(ag.name) = LOWER($2) OR LOWER(ag.canonical_name) = LOWER($2)) "
            "  AND a.range_start_at = $3 AND a.range_end_at = $4 "
            "ORDER BY a.generated_at DESC LIMIT 1",
            config.team_id, agent_name.strip(), range_start, range_end,
        )
        if row is None:
            return None
        sections = await conn.fetch(
            "SELECT section_id, section_name, section_number, trend, "
            "       summary, coaching_tip "
            "FROM qa.assessment_sections WHERE assessment_id = $1 "
            "ORDER BY section_number",
            row["id"],
        )
    return {**dict(row), "sections": [dict(s) for s in sections]}

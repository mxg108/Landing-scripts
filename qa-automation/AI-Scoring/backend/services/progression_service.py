"""Agent progression assessment service.

Generation goes through the llm/ provider seam (ModelProviderDesign):
Gemini by default with the team JSON's existing knobs; flip to Claude
via PROGRESSION_MODEL_PROVIDER=anthropic — no code change.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Optional, TYPE_CHECKING

from backend.services.llm.factory import resolve_stage

from backend.models.dashboard import (
    EvaluationRecord,
    ProgressionAssessment,
    SectionAssessment,
)
from backend.prompts.progression_prompt import build_progression_prompt

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from backend.config.team_config import TeamConfig

CACHE_TTL_SECONDS = 3600  # 1 hour

# Simple in-memory TTL cache: {(agent_name_lower, days): (timestamp, result)}
_cache: dict[tuple[str, int], tuple[float, ProgressionAssessment]] = {}


def _get_cached(agent_name: str, days: int) -> Optional[ProgressionAssessment]:
    """Return cached result if it exists and hasn't expired."""
    key = (agent_name.lower(), days)
    entry = _cache.get(key)
    if entry is None:
        return None
    cached_at, result = entry
    if time.time() - cached_at > CACHE_TTL_SECONDS:
        del _cache[key]
        return None
    return result


def _set_cached(agent_name: str, days: int, result: ProgressionAssessment) -> None:
    """Store a result in the cache."""
    key = (agent_name.lower(), days)
    _cache[key] = (time.time(), result)


def _serialize_records(records: list[EvaluationRecord]) -> str:
    """Serialize evaluation records to JSON for the Gemini prompt."""
    serialized = []
    for rec in records:
        entry: dict = {
            "date": rec.timestamp.strftime("%Y-%m-%d"),
            "overall_score": rec.overall_score,
            "sections": {},
        }
        for sec_name, sec_data in rec.sections.items():
            section_data: dict = {"score": sec_data.score}
            if sec_data.confidence:
                section_data["confidence"] = sec_data.confidence
            if sec_data.reasoning:
                section_data["reasoning"] = sec_data.reasoning
            entry["sections"][sec_name] = section_data

        if rec.key_strengths:
            entry["key_strengths"] = rec.key_strengths
        if rec.improvements:
            entry["improvements"] = rec.improvements

        serialized.append(entry)

    return json.dumps(serialized, indent=2)


def _strip_markdown_fences(text: str) -> str:
    """Remove markdown code fences from Gemini response if present."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


async def get_progression(
    provider,
    agent_name: str,
    days: int = 30,
    config: TeamConfig | None = None,
) -> ProgressionAssessment:
    """Generate a Gemini-powered progression assessment for an agent.

    Args:
        provider: A data provider with ``name`` and ``get_agent_history`` attrs.
        agent_name: The agent's display name.
        days: Number of days of history to analyze (default 30).
        config: Team configuration. If None, loads the default.

    Returns:
        A ProgressionAssessment with overall assessment and per-section
        coaching insights.
    """
    if config is None:
        from backend.config.team_config import get_team_config
        config = get_team_config()

    cached = _get_cached(agent_name, days)
    if cached is not None:
        return cached

    records: list[EvaluationRecord] = await provider.get_agent_history(
        agent_name, days
    )

    if not records:
        result = ProgressionAssessment(
            overall_assessment=(
                f"No evaluations found for {agent_name} in the last {days} days."
            ),
            section_assessments={},
            evaluation_count=0,
            time_range_days=days,
            data_source=provider.name,
        )
        _set_cached(agent_name, days, result)
        return result

    try:
        result = await generate_from_records(records, agent_name, days, config,
                                             data_source=provider.name)
    except AssessmentParseError:
        # parse-failure placeholder — cached (avoids hammering Gemini) but
        # never persisted (§Q4.a: only genuine AI output lands).
        result = ProgressionAssessment(
            overall_assessment=(
                f"Assessment generation for {agent_name} produced a partial response. "
                "Try a shorter time window or retry."
            ),
            section_assessments={},
            evaluation_count=len(records),
            time_range_days=days,
            data_source=provider.name,
        )
        _set_cached(agent_name, days, result)
        return result

    # R3 (JulyR2R3 §2): persist every FRESH generation — cache hits above
    # never reach here, and the two placeholder results return early, so
    # only genuine AI output lands in qa.assessments. Failures are logged
    # and swallowed: the dashboard card renders regardless.
    try:
        from backend.services.assessment_store import persist_assessment
        await persist_assessment(result, agent_name=agent_name, config=config)
    except Exception:  # noqa: BLE001 — persistence must not break the card
        logger.exception(
            "progression: assessment persist failed for %r — card served "
            "without a durable row", agent_name,
        )

    _set_cached(agent_name, days, result)
    return result


class AssessmentParseError(Exception):
    """The model's response wasn't valid JSON — no genuine assessment exists."""


async def generate_from_records(
    records: list[EvaluationRecord],
    agent_name: str,
    days: int,
    config: TeamConfig,
    data_source: str = "PostgreSQL",
) -> ProgressionAssessment:
    """The generation core: serialize → prompt → model → parse. Shared by
    the dashboard path (get_progression, rolling window) and the EOM
    one-pager (calendar-month records). No caching, no persistence —
    callers own both. Raises AssessmentParseError on unparseable output."""
    evaluations_json = _serialize_records(records)
    prompt = build_progression_prompt(config, agent_name, evaluations_json, days)

    stage = resolve_stage("progression", config)
    result = await stage.provider.generate(
        prompt,
        model=stage.model,
        max_output_tokens=stage.max_output_tokens,
    )

    raw_text = _strip_markdown_fences(result.text)
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise AssessmentParseError(str(exc)) from exc

    # Convert list response to dict keyed by internal history IDs
    section_name_map = config.section_name_to_history_id
    section_assessments: dict[str, SectionAssessment] = {}
    for sa in parsed.get("section_assessments", []):
        display_name = sa.get("section_name", "")
        key = section_name_map.get(display_name, display_name.lower())
        section_assessments[key] = SectionAssessment(
            trend=sa["trend"],
            summary=sa["summary"],
            coaching_tip=sa["coaching_tip"],
        )

    return ProgressionAssessment(
        overall_assessment=parsed["overall_assessment"],
        section_assessments=section_assessments,
        evaluation_count=len(records),
        time_range_days=days,
        data_source=data_source,
    )

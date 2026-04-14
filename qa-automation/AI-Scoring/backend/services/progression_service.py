"""Gemini-powered agent progression assessment service."""

from __future__ import annotations

import json
import os
import re
import time
from typing import Optional, TYPE_CHECKING

from google import genai
from google.genai import types

from backend.models.dashboard import (
    EvaluationRecord,
    ProgressionAssessment,
    SectionAssessment,
)
from backend.prompts.progression_prompt import build_progression_prompt

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

    evaluations_json = _serialize_records(records)
    prompt = build_progression_prompt(config, agent_name, evaluations_json, days)

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set in environment")

    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=config.gemini.progression_model,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=config.gemini.progression_temperature,
            max_output_tokens=config.gemini.progression_max_output_tokens,
        ),
    )

    raw_text = _strip_markdown_fences(response.text)
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError:
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

    result = ProgressionAssessment(
        overall_assessment=parsed["overall_assessment"],
        section_assessments=section_assessments,
        evaluation_count=len(records),
        time_range_days=days,
        data_source=provider.name,
    )

    _set_cached(agent_name, days, result)
    return result

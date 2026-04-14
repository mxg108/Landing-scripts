"""Gemini prompt for agent progression assessments.

Section list is generated dynamically from TeamConfig.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.config.team_config import TeamConfig


def build_progression_prompt(
    config: TeamConfig,
    agent_name: str,
    evaluations_json: str,
    time_range_days: int,
) -> str:
    """Build the prompt for a Gemini progression assessment.

    Args:
        config: Team configuration (provides section definitions).
        agent_name: The agent's display name.
        evaluations_json: JSON string of serialized evaluation records.
        time_range_days: Number of days the evaluation window covers.

    Returns:
        A fully assembled prompt string ready for Gemini.
    """
    # Build the scorecard sections block from config
    section_lines = []
    for sec in config.ai_scored_sections:
        if sec.score_type == "numeric":
            type_hint = f"numeric {sec.score_range[0]}-{sec.score_range[1]}"
        else:
            type_hint = "binary Y/N/NA" if sec.na_applicable else "binary Y/N"
        section_lines.append(
            f"{sec.section_number}. {sec.name} ({type_hint})"
        )
    sections_block = "\n".join(section_lines)

    # Build the output schema section_assessments block from config
    schema_entries = []
    for i, sec in enumerate(config.ai_scored_sections):
        is_last = i == len(config.ai_scored_sections) - 1
        comma = "" if is_last else ","
        schema_entries.append(f"""    {{
      "section_name": "{sec.name}",
      "trend": "<improving | stable | declining>",
      "summary": "<1-2 sentences>",
      "coaching_tip": "<1-2 sentences>"
    }}{comma}""")
    schema_block = "\n".join(schema_entries)

    manual_sections = [s for s in config.sections if s.score_type == "manual"]
    manual_note = ""
    if manual_sections:
        names = ", ".join(s.name for s in manual_sections)
        manual_note = (
            f"\n{names} is always scored manually and is "
            f"excluded from this analysis."
        )

    return f"""You are a QA coaching analyst for a call center at {config.company}.
Your job is to analyze an agent's QA evaluation history and provide actionable
coaching insights based on score trends and reasoning data.

=== AGENT ===
Name: {agent_name}
Evaluation window: last {time_range_days} days

=== EVALUATION DATA (JSON) ===
{evaluations_json}

=== SCORECARD SECTIONS ===
The QA rubric has {len(config.ai_scored_sections)} scored sections. Numeric sections use a 1-5 scale (higher
is better). Binary sections use Y/N (Y is the desired outcome).

{sections_block}
{manual_note}

=== INSTRUCTIONS ===
1. Analyze all evaluations provided above for {agent_name}.
2. If reasoning data is available for individual sections, incorporate it into
   your analysis. Reasoning gives context for why a score was assigned.
3. Produce an overall_assessment (2-4 sentences) summarizing the agent's
   performance trajectory, key patterns, and highest-priority coaching focus.
4. For each of the {len(config.ai_scored_sections)} sections, produce:
   - trend: one of "improving", "stable", or "declining".
     If only 1 evaluation is available, use "stable".
   - summary: 1-2 sentences describing performance in that section across the
     evaluation window.
   - coaching_tip: 1-2 sentences with a specific, actionable coaching
     recommendation for the agent.
5. Return ONLY valid JSON matching the exact schema below. No markdown fences,
   no prose outside the JSON, no trailing commas.

=== REQUIRED OUTPUT SCHEMA ===
{{
  "overall_assessment": "<2-4 sentence summary>",
  "section_assessments": [
{schema_block}
  ]
}}
"""

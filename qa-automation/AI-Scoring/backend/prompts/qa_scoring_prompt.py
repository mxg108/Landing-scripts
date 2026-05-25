"""QA scoring prompts for Gemini.

Prompt text is generated dynamically from TeamConfig so that each team's
rubric, section order, and scoring rules are defined in JSON — not here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.config.team_config import TeamConfig


# ---------------------------------------------------------------------------
# Dynamic prompt builders (from TeamConfig)
# ---------------------------------------------------------------------------

# Rules that apply to every Landing team, appended after each team's
# system_prompt_template. Kept here (not duplicated per-team JSON) so a
# single edit propagates to every team's prompt.
LANDING_GENERAL_INSTRUCTIONS = """
=== GENERAL LANDING RULES ===
- Agent name in the greeting: an agent may introduce themselves with any
  name they choose (some teams have multiple people with the same first
  name, so reps may differentiate via a preferred or alternate name). Do
  NOT penalize the agent for the specific name used in the greeting. Do
  flag if the agent uses one name in the greeting and then switches to a
  different name later in the same call — consistency within the call is
  required. The agent's Dialpad-recorded name is internal and is NOT the
  ground truth for what name they must use with the lead/member.
""".rstrip()


def build_system_prompt(config: TeamConfig) -> str:
    """Render the system-level instruction from config template, then append
    the Landing-wide general rules block."""
    rendered = config.scoring_prompt.system_prompt_template.format(
        company=config.company,
    )
    return f"{rendered}\n\n{LANDING_GENERAL_INSTRUCTIONS}"


def build_scoring_rubric(config: TeamConfig) -> str:
    """Generate the === SCORING RUBRIC === block from config sections."""
    lines = ["\n=== SCORING RUBRIC ===\n"]

    for sec in config.sections:
        if sec.score_type in ("manual", "manual_yn"):
            lines.append(
                f"SECTION {sec.section_number} — {sec.name} "
                f"(SKIP — always manual, never scored by AI)\n"
            )
            continue

        # Section header with score type hint
        if sec.score_type == "yn":
            header = (
                f"SECTION {sec.section_number} — {sec.name} "
                f'(yn_value: "Y", "N", or "NA")'
            )
        else:
            header = (
                f"SECTION {sec.section_number} — {sec.name} "
                f"(score: {sec.score_range[0]}–{sec.score_range[1]})"
            )
        if sec.audio_dependent:
            header += " [AUDIO-DEPENDENT]"
        lines.append(header)

        # Rubric question
        if sec.rubric_question:
            lines.append(sec.rubric_question)

        # Score descriptions (for numeric sections)
        if sec.score_descriptions:
            for key, desc in sec.score_descriptions.items():
                lines.append(f"{key}: {desc}")

        # NA note — applies to any section flagged na_applicable, including
        # numeric. For numeric NA, the model emits yn_value="NA" and leaves
        # score null.
        if sec.na_applicable and sec.score_type in ("yn", "numeric"):
            lines.append(
                "Mark NA if not applicable (e.g. internal call or "
                "no sensitive info discussed)."
            )

        lines.append("")  # blank line between sections

    # Confidence levels
    lines.append("=== CONFIDENCE LEVELS ===")
    lines.append(config.scoring_prompt.confidence_levels_note)
    lines.append("")

    # Confidence cap rule
    capped = [s for s in config.ai_scored_sections if s.confidence_cap]
    if capped:
        numbers = " and ".join(str(s.section_number) for s in capped)
        cap_level = capped[0].confidence_cap  # all use same cap level
        lines.append(
            f'For Sections {numbers}: cap confidence at "{cap_level}" '
            f"unless there is unambiguous audio evidence."
        )

    return "\n".join(lines)


def build_output_schema(config: TeamConfig) -> str:
    """Generate the === REQUIRED OUTPUT FORMAT === block from config sections."""
    lines = [
        "\n=== REQUIRED OUTPUT FORMAT ===",
        "Return exactly this JSON structure and nothing else:\n",
        "{",
        '  "sections": [',
    ]

    for i, sec in enumerate(config.ai_scored_sections):
        is_last = i == len(config.ai_scored_sections) - 1

        if sec.score_type == "numeric":
            assert sec.score_range is not None, f"numeric section {sec.id} missing score_range"
            lo, hi = sec.score_range
            score_type_str = "numeric"
            if sec.na_applicable:
                score_val = f"<{lo}-{hi} integer, or null if NA>"
                yn_val = "<null or NA>"
            else:
                score_val = f"<{lo}-{hi} integer>"
                yn_val = "null"
        else:
            score_val = "null"
            yn_val = "<Y or N or NA>" if sec.na_applicable else "<Y or N>"
            score_type_str = "yn"

        # Build reasoning instruction
        reasoning = "<one or two sentences>"
        if sec.special_reasoning_instructions:
            reasoning = (
                f"<one or two sentences, {sec.special_reasoning_instructions}>"
            )

        # yn_value: null for numeric, "<Y or N or NA>" for Y/N (keep angle brackets)
        if yn_val == "null":
            yn_field = "null"
        else:
            yn_field = f'"{yn_val}"'

        lines.append("    {")
        lines.append(f'      "id": "{sec.id}",')
        lines.append(f'      "name": "{sec.name}",')
        lines.append(f'      "score": {score_val},')
        lines.append(f'      "score_type": "{score_type_str}",')
        lines.append(f'      "yn_value": {yn_field},')
        lines.append(f'      "confidence": "<high or medium or low>",')
        lines.append(f'      "reasoning": "{reasoning}",')
        lines.append(
            f'      "audio_dependent": {"true" if sec.audio_dependent else "false"},'
        )
        lines.append(f'      "flags": []')
        comma = "" if is_last else ","
        lines.append(f"    }}{comma}")

    lines.append("  ],")
    lines.append(
        '  "call_summary": "<a concise summary of the call, '
        'in one or two sentences>",'
    )
    lines.append(
        '  "key_strengths": "<2-3 specific strengths observed '
        'in this call, as a single string>",'
    )
    lines.append(
        '  "opportunities": "<2-3 specific coaching opportunities '
        'from this call, as a single string>"'
    )
    lines.append("}")

    return "\n".join(lines)


SOP_CONTEXT_BLOCK = """
=== SOP CONTEXT ({sop_title}) ===
Use the following Standard Operating Procedure to evaluate Sections 5 (Process Adherence)
and 6 (Call Resolution). Score these sections against this policy, not general knowledge.

{sop_content}
"""

TRANSCRIPT_CONTEXT_BLOCK = """
=== DIALPAD TRANSCRIPT ===
Use this alongside the audio to improve accuracy. Speaker labels and content are from Dialpad.

{transcript_text}

=== DIALPAD SIGNAL MOMENTS ===
These are events automatically detected by Dialpad during the call:
{moments_text}
"""


def _build_sop_missing_note(config: TeamConfig) -> str:
    """Generate the SOP-missing fallback note from config."""
    sop_nums = config.scoring_prompt.sop_sections
    section_refs = " and ".join(str(n) for n in sop_nums)
    return (
        f"\n[No SOP context loaded. Score Sections {section_refs} "
        f"conservatively and add 'sop_context_missing' to their flags.]\n"
    )


def build_prompt(
    config: TeamConfig,
    transcript_text: str = "",
    moments_text: str = "",
    sop_title: str = "",
    sop_content: str = "",
    agent_name: str = "",
    extra_notes: str = "",
) -> str:
    """Assemble the full user prompt for a scoring request."""
    parts = [build_scoring_rubric(config)]

    if sop_content:
        parts.append(
            SOP_CONTEXT_BLOCK.format(sop_title=sop_title, sop_content=sop_content)
        )
    else:
        parts.append(_build_sop_missing_note(config))

    if transcript_text:
        moments_str = moments_text if moments_text else "No moments detected."
        parts.append(
            TRANSCRIPT_CONTEXT_BLOCK.format(
                transcript_text=transcript_text, moments_text=moments_str
            )
        )

    if agent_name:
        parts.append(f"\nAgent name: {agent_name}")
    if extra_notes:
        parts.append(f"Additional context: {extra_notes}")

    parts.append(build_output_schema(config))
    parts.append(
        "\n[Audio attached — score the call based on what you hear; the "
        "transcript is secondary to the audio source. If the call is in "
        "SPANISH, ignore the transcript and base your scores solely on the "
        "audio. DO NOT mention a lack of SOP in your score reasoning if no "
        "SOP context was provided.]"
    )

    return "\n".join(parts)

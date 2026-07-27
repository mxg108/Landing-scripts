"""Stage-B judge prompt — TwoStageScoringDesign §4.

The judge scores the call FROM the rendered annotated transcript — it
never hears audio. Everything rubric-shaped is reused verbatim from
qa_scoring_prompt (rubric, SOP block + missing-note, output schema) so
single-stage and two-stage judges score against byte-identical
instructions; only the evidence block and the source-of-truth doctrine
differ.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from backend.prompts.qa_scoring_prompt import (
    SOP_CONTEXT_BLOCK,
    _build_sop_missing_note,
    _sop_section_refs,
    build_output_schema,
    build_scoring_rubric,
)

if TYPE_CHECKING:
    from backend.config.team_config import TeamConfig


# Keep the agent-name rule in sync with qa_scoring_prompt's
# LANDING_GENERAL_INSTRUCTIONS — a test pins the two together. Only the
# source-of-truth bullet differs: the judge's SOT is the annotated
# record, not audio it cannot hear.
JUDGE_GENERAL_INSTRUCTIONS = """
=== GENERAL LANDING RULES ===
- Agent name in the greeting: an agent may introduce themselves with any
  name they choose (some teams have multiple people with the same first
  name, so reps may differentiate via a preferred or alternate name). Do
  NOT penalize the agent for the specific name used in the greeting. Do
  flag if the agent uses one name in the greeting and then switches to a
  different name later in the same call — consistency within the call is
  required. The agent's Dialpad-recorded name is internal and is NOT the
  ground truth for what name they must use with the lead/member.
- Source of truth: the ANNOTATED CALL RECORD is the authoritative
  account of the call. It was produced by an audio-native annotator
  that listened to the full recording — including for non-English
  calls, where the annotator re-transcribed from the audio. Score from
  the record; there is no separate transcript to consult.
""".rstrip()


ANNOTATION_CONTEXT_BLOCK = """
=== ANNOTATED CALL RECORD (authoritative) ===
You cannot hear the call. The record below is the authoritative account
of it, produced by an audio-native annotator. How to read it:
- Per-turn emotion / pace / [interrupts] tags are audio-derived
  observations — use them as your evidence for tone, empathy, and
  pacing judgments.
- HOLD lines are labeled "observational — not system-verified": the
  annotator HEARD them (music, dead air). Treat them as observations;
  only the CALL CONTEXT block above (when present) contains verified
  system data.
- CALL-LEVEL OBSERVATIONS carry context per-turn fields can't (audio
  quality, tone arcs, transcript divergences).
- An "ANNOTATION TRUNCATED" observation means the record is partial:
  score what the record shows, mark LOW confidence on any section the
  missing tail could change, and say so in that section's reasoning.

{annotation_text}
"""


def build_judge_system_prompt(config: "TeamConfig") -> str:
    """Team persona template + the judge variant of the general rules."""
    rendered = config.scoring_prompt.system_prompt_template.format(
        company=config.company,
    )
    return f"{rendered}\n\n{JUDGE_GENERAL_INSTRUCTIONS}"


def build_judge_prompt(
    config: "TeamConfig",
    annotation_text: str,
    sop_title: str = "",
    sop_content: str = "",
    agent_name: str = "",
    extra_notes: str = "",
    call_context_text: str = "",
) -> str:
    """Assemble the Stage-B user prompt — build_prompt's shape with the
    annotated record standing where transcript + audio stood."""
    parts = [build_scoring_rubric(config)]

    if sop_content:
        parts.append(
            SOP_CONTEXT_BLOCK.format(
                sop_title=sop_title,
                sop_section_refs=_sop_section_refs(config),
                sop_content=sop_content,
            )
        )
    else:
        parts.append(_build_sop_missing_note(config))

    # DispositionDesign §5: verified system data rides AHEAD of the
    # call record, exactly as it rides ahead of the transcript today.
    if call_context_text:
        parts.append(call_context_text)

    parts.append(ANNOTATION_CONTEXT_BLOCK.format(annotation_text=annotation_text))

    if agent_name:
        parts.append(f"\nAgent name: {agent_name}")
    if extra_notes:
        parts.append(f"Additional context: {extra_notes}")

    parts.append(build_output_schema(config))
    parts.append(
        "\n[No audio is attached. Score exclusively from the ANNOTATED "
        "CALL RECORD and the context blocks above. DO NOT mention a lack "
        "of SOP in your score reasoning if no SOP context was provided.]"
    )

    return "\n".join(parts)

"""Stage-B judge — TwoStageScoringDesign §4.

Scores a call FROM its Stage-A annotated transcript through the llm/
provider seam: any TextModelProvider can be the judge. Resolution is
env-keyed (SCORING_MODEL_PROVIDER = gemini | anthropic, default gemini
— the shadow rollout judges with Gemini first to isolate the split's
effect from the model's, then flips to Claude via
SCORING_MODEL_PROVIDER=anthropic / SCORING_ANTHROPIC_MODEL).

Output parsing matches the single-stage path byte-for-byte: fence-
tolerant JSON extraction + Scorecard validation against the team's
section definitions. Raises on failure — the CALLER (scoring_service)
owns fallback semantics (two_stage falls back to single-stage
score_audio; shadow logs and stamps the error).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from backend.models.formula import AnnotatedTranscript
from backend.models.scorecard import Scorecard
from backend.prompts.judge_prompt import build_judge_prompt, build_judge_system_prompt
from backend.services.annotation_render import render_annotated_transcript
from backend.services.audio_service import _extract_json
from backend.services.llm.factory import resolve_stage
from backend.services.llm.provider import LlmResult

if TYPE_CHECKING:
    from backend.config.team_config import TeamConfig

logger = logging.getLogger(__name__)


async def score_annotation(
    annotation: AnnotatedTranscript,
    config: "TeamConfig",
    *,
    sop_title: str = "",
    sop_content: str = "",
    agent_name: str = "",
    extra_notes: str = "",
    call_context_text: str = "",
) -> tuple[Scorecard, LlmResult]:
    """Render the annotation, build the judge prompt, generate, validate.

    Returns the validated Scorecard plus the LlmResult whose
    provider/model feed the models_used text-leg stamp."""
    annotation_text = render_annotated_transcript(annotation)
    prompt = build_judge_prompt(
        config,
        annotation_text,
        sop_title=sop_title,
        sop_content=sop_content,
        agent_name=agent_name,
        extra_notes=extra_notes,
        call_context_text=call_context_text,
    )

    stage = resolve_stage("scoring", config)
    result = await stage.provider.generate(
        prompt,
        model=stage.model,
        max_output_tokens=stage.max_output_tokens,
        system=build_judge_system_prompt(config),
    )

    sections_by_id = {s.id: s for s in config.sections}
    scorecard = Scorecard.model_validate(
        _extract_json(result.text), context={"sections_by_id": sections_by_id}
    )
    return scorecard, result

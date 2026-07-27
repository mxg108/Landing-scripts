"""Stage → provider resolution (mirrors rag/factory's env-keyed seam).

A *stage* is one text-generation job in the pipeline: ``progression``
(the dashboard card + EOM one-pager) and ``scoring`` (the Stage-B judge
— TwoStageScoringDesign §4). Resolution order per stage:

  1. env override  — ``{STAGE}_MODEL_PROVIDER`` = gemini | anthropic
     (unknown values collapse to the default, house pattern)
  2. default       — gemini with the team JSON's existing ``gemini.*``
     knobs: no team JSON changes, no behavior changes until someone
     flips the env var. For scoring this also matches the shadow
     rollout order — Gemini-judge first isolates the split's effect on
     scores before the Claude flip isolates the model's (§6).

Anthropic model per stage comes from ``{STAGE}_ANTHROPIC_MODEL``
(default ``claude-sonnet-5`` — the §10 starting tier; the newest Opus
tier is the intended progression upgrade later, never Fable).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from backend.services.llm.anthropic import AnthropicTextProvider
from backend.services.llm.gemini import GeminiTextProvider
from backend.services.llm.provider import TextModelProvider

if TYPE_CHECKING:
    from backend.config.team_config import TeamConfig

_DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-5"


@dataclass
class StageModel:
    """Everything a call site needs to run one stage's generation."""
    provider: TextModelProvider
    model: str
    max_output_tokens: int


def _env_provider(stage: str) -> str:
    raw = os.environ.get(f"{stage.upper()}_MODEL_PROVIDER", "").strip().lower()
    return raw if raw in ("gemini", "anthropic") else "gemini"


def resolve_stage(stage: str, config: "TeamConfig") -> StageModel:
    """Resolve *stage* ("progression" | "scoring") to a constructed
    provider + model.

    Providers are lightweight per-call constructs (the Gemini client is
    built per request exactly as the pre-seam code did; the Anthropic
    client is lazy inside its provider) — no singleton needed yet.
    """
    if stage == "progression":
        gemini_knobs = (
            config.gemini.progression_model,
            config.gemini.progression_temperature,
            config.gemini.progression_max_output_tokens,
        )
    elif stage == "scoring":
        gemini_knobs = (
            config.gemini.scoring_model,
            config.gemini.scoring_temperature,
            config.gemini.scoring_max_output_tokens,
        )
    else:
        raise ValueError(f"unknown llm stage: {stage!r}")

    gemini_model, gemini_temperature, max_output_tokens = gemini_knobs
    provider_name = _env_provider(stage)
    if provider_name == "anthropic":
        env_model = os.environ.get(f"{stage.upper()}_ANTHROPIC_MODEL", "").strip()
        return StageModel(
            provider=AnthropicTextProvider(),
            model=env_model or _DEFAULT_ANTHROPIC_MODEL,
            max_output_tokens=max_output_tokens,
        )
    return StageModel(
        provider=GeminiTextProvider(temperature=gemini_temperature),
        model=gemini_model,
        max_output_tokens=max_output_tokens,
    )

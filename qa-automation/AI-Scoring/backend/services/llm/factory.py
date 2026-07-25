"""Stage → provider resolution (mirrors rag/factory's env-keyed seam).

A *stage* is one text-generation job in the pipeline: ``progression``
today; ``scoring`` (the Stage-B judge) lands with TwoStageScoringDesign
P3. Resolution order per stage:

  1. env override  — ``PROGRESSION_MODEL_PROVIDER`` = gemini | anthropic
     (unknown values collapse to the default, house pattern)
  2. default       — gemini with the team JSON's existing ``gemini.*``
     knobs, so day one is a pure refactor: no team JSON changes, no
     behavior changes until someone flips the env var.

Anthropic model per stage comes from ``PROGRESSION_ANTHROPIC_MODEL``
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
    """Resolve *stage* ("progression") to a constructed provider + model.

    Providers are lightweight per-call constructs (the Gemini client is
    built per request exactly as the pre-seam code did; the Anthropic
    client is lazy inside its provider) — no singleton needed yet.
    """
    if stage != "progression":
        raise ValueError(f"unknown llm stage: {stage!r}")

    provider_name = _env_provider(stage)
    if provider_name == "anthropic":
        model = os.environ.get(
            "PROGRESSION_ANTHROPIC_MODEL", _DEFAULT_ANTHROPIC_MODEL
        ).strip() or _DEFAULT_ANTHROPIC_MODEL
        return StageModel(
            provider=AnthropicTextProvider(),
            model=model,
            max_output_tokens=config.gemini.progression_max_output_tokens,
        )
    return StageModel(
        provider=GeminiTextProvider(
            temperature=config.gemini.progression_temperature
        ),
        model=config.gemini.progression_model,
        max_output_tokens=config.gemini.progression_max_output_tokens,
    )

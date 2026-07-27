"""Neutral text-model contract — no vendor imports live here.

Deliberately text-only (``generate(prompt) -> LlmResult``): that covers
progression + the EOM one-pager today and the Stage-B scorer next,
without inventing an audio interface nobody can implement twice
(TwoStageScoringDesign §2 — audio stays on the Stage-A annotator).

Sampling knobs (temperature etc.) stay OUT of the contract — current
Claude models reject them; each provider module maps its own knobs at
construction time (ModelProviderDesign §3).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


class LlmProviderError(Exception):
    """The provider could not produce usable text (API error, refusal,
    missing credentials, unsupported feature). Callers decide whether
    that means fallback, placeholder, or raise — the seam never does."""


@dataclass
class LlmResult:
    """One completed generation, with provenance for models_used stamps."""
    text: str
    provider: str      # "gemini" | "anthropic"
    model: str         # the model that actually served the request


class TextModelProvider(ABC):
    """One vendor's text-generation surface behind the neutral contract."""

    name: str = "unknown"

    @abstractmethod
    async def generate(
        self,
        prompt: str,
        *,
        model: str,
        max_output_tokens: int,
        json_schema: Optional[dict] = None,
        system: Optional[str] = None,
    ) -> LlmResult:
        """Generate text for *prompt* on *model*.

        ``json_schema`` requests provider-enforced structured output.
        Providers that cannot enforce it MUST raise LlmProviderError
        rather than silently returning free text — a scorecard parsed
        from unconstrained output is a different reliability class and
        the caller needs to know.

        ``system`` is the system-level instruction (the Stage-B judge
        carries the team's scoring persona here); providers map it to
        their native channel (Gemini system_instruction / Anthropic
        system param).
        """
        ...

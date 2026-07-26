"""Anthropic (Claude) text provider — the ONLY anthropic-SDK-shaped
module in the backend (grep-gated, same rule as pulpo.py for the RAG
seam).

Model doctrine (TwoStageScoringDesign §10, owner 2026-07-25):
``claude-sonnet-5`` is the starting judge; the newest Opus tier is
reserved for progression analysis later; **never Fable**. Current
Claude models reject sampling params (temperature/top_p) — none are
sent. Adaptive thinking is set explicitly so behavior is identical
across Sonnet 5 and Opus-tier models.

Requires ``ANTHROPIC_API_KEY`` in the environment (Railway + .env —
owner adds it; until then any call raises LlmProviderError cleanly).
"""

from __future__ import annotations

import os
from typing import Optional

from backend.services.llm.provider import (
    LlmProviderError,
    LlmResult,
    TextModelProvider,
)


class AnthropicTextProvider(TextModelProvider):
    name = "anthropic"

    def __init__(self, client=None):
        """*client* is a test seam; production constructs lazily from env
        (lazy so the `anthropic` package is only required when this
        provider is actually selected)."""
        self._client = client

    def _get_client(self):
        if self._client is not None:
            return self._client
        # Key resolution: engineering-provisioned key wins, then the
        # Landing key, then the owner's personal key (funded interim,
        # 2026-07-26). Sub-20-char values are placeholders (the .env
        # carries a stub _LANDING entry until engineering delivers) —
        # skipped so the first REAL key wins without editing .env.
        api_key = next(
            (
                key for key in (
                    os.getenv("ANTHROPIC_API_KEY"),
                    os.getenv("ANTHROPIC_API_KEY_LANDING"),
                    os.getenv("ANTHROPIC_API_KEY_PERSONAL"),
                )
                if key and len(key) >= 20
            ),
            None,
        )
        if not api_key:
            raise LlmProviderError(
                "no Anthropic key in environment (ANTHROPIC_API_KEY / "
                "_LANDING / _PERSONAL)"
            )
        from anthropic import AsyncAnthropic
        self._client = AsyncAnthropic(api_key=api_key)
        return self._client

    async def generate(
        self,
        prompt: str,
        *,
        model: str,
        max_output_tokens: int,
        json_schema: Optional[dict] = None,
    ) -> LlmResult:
        client = self._get_client()
        kwargs: dict = {
            "model": model,
            "max_tokens": max_output_tokens,
            "thinking": {"type": "adaptive"},
            "messages": [{"role": "user", "content": prompt}],
        }
        if json_schema is not None:
            # Provider-enforced structured output — replaces markdown-fence
            # stripping entirely on this path (guaranteed-valid JSON).
            kwargs["output_config"] = {
                "format": {"type": "json_schema", "schema": json_schema}
            }
        response = await client.messages.create(**kwargs)

        if response.stop_reason == "refusal":
            raise LlmProviderError(f"model {model} refused the request")
        text = "".join(
            block.text for block in response.content if block.type == "text"
        )
        if not text:
            raise LlmProviderError(
                f"model {model} returned no text (stop_reason="
                f"{response.stop_reason!r})"
            )
        return LlmResult(
            text=text, provider=self.name, model=getattr(response, "model", model)
        )

"""Gemini text provider — the google-genai client code extracted from
progression_service (P1 pure refactor: same client, same knobs, same
missing-key error message; only the module moved).

The ONLY other google-genai importer is audio_service (the Stage-A
audio leg, which is Gemini-native by design and outside this seam).
"""

from __future__ import annotations

import os
from typing import Optional

from google import genai
from google.genai import types

from backend.services.llm.provider import (
    LlmProviderError,
    LlmResult,
    TextModelProvider,
)


class GeminiTextProvider(TextModelProvider):
    name = "gemini"

    def __init__(self, temperature: Optional[float] = None, client=None):
        """*temperature* is a Gemini-specific knob mapped at construction
        (the neutral contract carries none — ModelProviderDesign §3).
        *client* is a test seam; production constructs from env."""
        self._temperature = temperature
        self._client = client

    def _get_client(self):
        if self._client is not None:
            return self._client
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY not set in environment")
        return genai.Client(api_key=api_key)

    async def generate(
        self,
        prompt: str,
        *,
        model: str,
        max_output_tokens: int,
        json_schema: Optional[dict] = None,
    ) -> LlmResult:
        if json_schema is not None:
            # Provider-enforced structured output lands with the Stage-B
            # scorer (TwoStageScoringDesign P3) — mapped against the live
            # SDK then, never silently ignored now (provider.py contract).
            raise LlmProviderError(
                "structured output not implemented for the gemini provider yet"
            )
        client = self._get_client()
        config = types.GenerateContentConfig(
            temperature=self._temperature,
            max_output_tokens=max_output_tokens,
        )
        response = await client.aio.models.generate_content(
            model=model, contents=prompt, config=config,
        )
        return LlmResult(text=response.text or "", provider=self.name, model=model)

"""Live smoke for the llm/ provider seam (ModelProviderDesign P1).

Usage (from qa-automation/AI-Scoring, .env loaded):

    .venv/bin/python scripts/llm_smoke.py                    # gemini
    .venv/bin/python scripts/llm_smoke.py --provider anthropic
    .venv/bin/python scripts/llm_smoke.py --provider anthropic --json

Gemini works today; anthropic needs ANTHROPIC_API_KEY (owner adds it —
TwoStageScoringDesign §10.1). --json exercises the structured-output
path (anthropic only until the Gemini mapping lands with P3).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from backend.services.llm.anthropic import AnthropicTextProvider  # noqa: E402
from backend.services.llm.gemini import GeminiTextProvider  # noqa: E402

_PROMPT = (
    "In exactly one sentence, describe what a QA analyst does at a "
    "residential-living company's member support team."
)
_JSON_PROMPT = "Give a one-sentence QA analyst job description."
_SCHEMA = {
    "type": "object",
    "properties": {"description": {"type": "string"}},
    "required": ["description"],
    "additionalProperties": False,
}
_MODELS = {"gemini": "gemini-2.5-flash", "anthropic": "claude-sonnet-5"}


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=("gemini", "anthropic"),
                        default="gemini")
    parser.add_argument("--model", default=None,
                        help=f"override the default ({_MODELS})")
    parser.add_argument("--json", action="store_true",
                        help="exercise the structured-output path")
    args = parser.parse_args()

    provider = (GeminiTextProvider() if args.provider == "gemini"
                else AnthropicTextProvider())
    model = args.model or _MODELS[args.provider]

    kwargs: dict = {"model": model, "max_output_tokens": 1024}
    prompt = _PROMPT
    if args.json:
        kwargs["json_schema"] = _SCHEMA
        prompt = _JSON_PROMPT

    print(f"→ {args.provider} / {model} (json={args.json})")
    result = await provider.generate(prompt, **kwargs)
    print(f"← provider={result.provider} model={result.model}")
    print(result.text)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

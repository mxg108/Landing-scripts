"""Live smoke for the llm/ provider seam (ModelProviderDesign P1).

Usage (from qa-automation/AI-Scoring, .env loaded):

    .venv/bin/python scripts/llm_smoke.py                    # gemini
    .venv/bin/python scripts/llm_smoke.py --provider anthropic
    .venv/bin/python scripts/llm_smoke.py --provider anthropic --json
    .venv/bin/python scripts/llm_smoke.py --provider anthropic --scorecard-schema

Gemini works today; anthropic needs ANTHROPIC_API_KEY (owner adds it —
TwoStageScoringDesign §10.1). --json exercises the structured-output
path with a toy schema (anthropic only until the Gemini mapping lands);
--scorecard-schema sends the REAL Scorecard.model_json_schema() the
judge now passes and round-trips the reply through
Scorecard.model_validate — proves the API accepts pydantic's schema
dialect end-to-end.
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
_SCORECARD_PROMPT = (
    "Produce a demo scorecard for a flawless IMAGINARY support call: "
    "two sections — id 'greeting' (name 'Greeting', score_type 'yn', "
    "yn_value 'Y', score null) and id 'comms' (name 'Communication', "
    "score_type 'numeric', score 5, yn_value null) — each with "
    "confidence 'high' and one-sentence reasoning; brief call_summary, "
    "key_strengths, opportunities."
)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=("gemini", "anthropic"),
                        default="gemini")
    parser.add_argument("--model", default=None,
                        help=f"override the default ({_MODELS})")
    parser.add_argument("--json", action="store_true",
                        help="exercise the structured-output path (toy schema)")
    parser.add_argument("--scorecard-schema", action="store_true",
                        help="structured output with the REAL judge schema "
                             "(Scorecard.model_json_schema) + validate reply")
    args = parser.parse_args()

    provider = (GeminiTextProvider() if args.provider == "gemini"
                else AnthropicTextProvider())
    model = args.model or _MODELS[args.provider]

    kwargs: dict = {"model": model, "max_output_tokens": 1024}
    prompt = _PROMPT
    if args.scorecard_schema:
        from backend.models.scorecard import Scorecard
        kwargs["json_schema"] = Scorecard.model_json_schema()
        kwargs["max_output_tokens"] = 8192
        prompt = _SCORECARD_PROMPT
    elif args.json:
        kwargs["json_schema"] = _SCHEMA
        prompt = _JSON_PROMPT

    print(f"→ {args.provider} / {model} "
          f"(json={args.json or args.scorecard_schema})")
    result = await provider.generate(prompt, **kwargs)
    print(f"← provider={result.provider} model={result.model}")
    print(result.text)
    if args.scorecard_schema:
        import json as _json
        from backend.models.scorecard import Scorecard
        scorecard = Scorecard.model_validate(_json.loads(result.text))
        print(f"✓ Scorecard.model_validate passed "
              f"({len(scorecard.sections)} sections)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

"""Gemini audio upload, scoring, and Stage-A annotation.

This module is the Gemini-native AUDIO leg: single-stage scoring
(`score_audio`, today's pipeline and the two-stage Plan-B fallback) and
the Stage-A annotator (`annotate_audio`, TwoStageScoringDesign §3) both
live here — the only google-genai importers besides llm/gemini.py.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from google import genai
from google.genai import types

from backend.prompts.annotator_prompt import (
    ANNOTATOR_RESPONSE_SCHEMA,
    build_annotator_prompt,
    build_annotator_system_prompt,
)
from backend.prompts.qa_scoring_prompt import build_prompt, build_system_prompt
from backend.models.formula import AnnotatedTranscript
from backend.models.scorecard import Scorecard

if TYPE_CHECKING:
    from backend.config.team_config import TeamConfig

logger = logging.getLogger(__name__)

SUPPORTED_MIME_TYPES = {
    ".mp3": "audio/mp3",
    ".mp4": "audio/mp4",
    ".m4a": "audio/mp4",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".flac": "audio/flac",
    ".webm": "audio/webm",
}


def _get_client() -> genai.Client:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set in .env")
    return genai.Client(api_key=api_key)


def _get_mime_type(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    if ext not in SUPPORTED_MIME_TYPES:
        raise ValueError(f"Unsupported audio format '{ext}'")
    return SUPPORTED_MIME_TYPES[ext]


def _extract_json(text: str) -> dict:
    text = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text.strip())
    start = text.find("{")
    end = text.rfind("}") + 1
    if start == -1 or end == 0:
        raise ValueError("No JSON object found in model response.")
    json_str = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text[start:end])
    try:
        return json.loads(json_str)
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON parse failed: {e}\n\nSnippet:\n{json_str[:500]}")


async def score_audio(
    audio_bytes: bytes,
    filename: str,
    config: TeamConfig,
    transcript_text: str = "",
    sop_title: str = "",
    sop_content: str = "",
    agent_name: str = "",
    extra_notes: str = "",
    call_context_text: str = "",
) -> Scorecard:
    """
    Upload audio to Gemini and score it.
    Returns a validated Scorecard model.
    """
    client = _get_client()
    mime_type = _get_mime_type(filename)

    # Write to temp file for upload
    with tempfile.NamedTemporaryFile(suffix=Path(filename).suffix, delete=False) as tmp:
        tmp.write(audio_bytes)
        tmp_path = tmp.name

    try:
        uploaded = client.files.upload(
            file=tmp_path,
            config=types.UploadFileConfig(mime_type=mime_type, display_name=filename),
        )

        prompt = build_prompt(
            config=config,
            transcript_text=transcript_text,
            sop_title=sop_title,
            sop_content=sop_content,
            agent_name=agent_name,
            extra_notes=extra_notes,
            call_context_text=call_context_text,
        )

        response = client.models.generate_content(
            model=config.gemini.scoring_model,
            contents=[
                types.Content(
                    role="user",
                    parts=[
                        types.Part.from_uri(file_uri=uploaded.uri, mime_type=mime_type),
                        types.Part.from_text(text=prompt),
                    ],
                )
            ],
            config=types.GenerateContentConfig(
                system_instruction=build_system_prompt(config),
                temperature=config.gemini.scoring_temperature,
                max_output_tokens=config.gemini.scoring_max_output_tokens,
            ),
        )

        raw = _extract_json(response.text)
        sections_by_id = {s.id: s for s in config.sections}
        return Scorecard.model_validate(
            raw, context={"sections_by_id": sections_by_id}
        )

    finally:
        # Clean up temp file and Gemini upload
        Path(tmp_path).unlink(missing_ok=True)
        try:
            client.files.delete(name=uploaded.name)
        except Exception:
            pass


DEFAULT_ANNOTATOR_MODEL = "gemini-2.5-flash"


def _finish_diagnostics(response) -> str:
    """Why did generation stop? Gemini can end a candidate early
    (MAX_TOKENS, SAFETY, RECITATION — hold-music lyrics trigger the
    latter) and `response.text` alone hides it; a truncated candidate
    then surfaces as a misleading JSON parse error. Best-effort string
    for exception messages and logs."""
    bits = []
    try:
        candidates = response.candidates or []
        if candidates:
            bits.append(f"finish_reason={candidates[0].finish_reason}")
        else:
            bits.append("no candidates")
        usage = getattr(response, "usage_metadata", None)
        if usage is not None:
            bits.append(
                "tokens(prompt={}, out={}, thoughts={})".format(
                    getattr(usage, "prompt_token_count", None),
                    getattr(usage, "candidates_token_count", None),
                    getattr(usage, "thoughts_token_count", None),
                )
            )
        feedback = getattr(response, "prompt_feedback", None)
        if feedback:
            bits.append(f"prompt_feedback={feedback}")
    except Exception:  # noqa: BLE001 — diagnostics must never mask the real error
        bits.append("diagnostics unavailable")
    return "; ".join(bits)


def annotator_model() -> str:
    """Stage-A model — env-tunable for the flash-vs-pro trial
    (TwoStageScoringDesign §10.4); team-JSON knob lands with P4."""
    return os.environ.get("ANNOTATOR_MODEL", "").strip() or DEFAULT_ANNOTATOR_MODEL


# Attempt temperatures: 0.0 for fidelity, then a small bump. Gemini's
# near-deterministic decoding at temp 0 can re-serve the SAME degenerate
# sample on retry (observed 2026-07-27 on a bilingual call: two runs,
# byte-identical truncated JSON) — the bump exists to break that loop.
_ANNOTATE_ATTEMPT_TEMPS = (0.0, 0.2)


async def _annotate_once(
    client, uploaded, mime_type: str, prompt: str, model: str, temperature: float
) -> AnnotatedTranscript:
    response = await client.aio.models.generate_content(
        model=model,
        contents=[
            types.Content(
                role="user",
                parts=[
                    types.Part.from_uri(file_uri=uploaded.uri, mime_type=mime_type),
                    types.Part.from_text(text=prompt),
                ],
            )
        ],
        config=types.GenerateContentConfig(
            system_instruction=build_annotator_system_prompt(),
            temperature=temperature,
            max_output_tokens=65_536,
            # Constrained decoding: the response is grammar-forced into
            # the annotation schema, so syntactically-malformed JSON
            # (the observed bilingual failure class) can't happen. The
            # hand-authored dict, not the pydantic class — Gemini's
            # OpenAPI subset rejects additionalProperties (400).
            response_mime_type="application/json",
            response_schema=ANNOTATOR_RESPONSE_SCHEMA,
        ),
    )
    try:
        # genai parses schema'd responses itself; fall back to the
        # fence-tolerant path if .parsed is absent (older SDKs, stubs).
        parsed = getattr(response, "parsed", None)
        if isinstance(parsed, AnnotatedTranscript):
            return parsed
        return AnnotatedTranscript.model_validate(_extract_json(response.text))
    except Exception as exc:
        raise ValueError(
            f"annotation unusable [{_finish_diagnostics(response)}]: {exc}"
        ) from exc


async def annotate_audio(
    audio_bytes: bytes,
    filename: str,
    transcript_text: str = "",
    moments_display: Optional[list[dict]] = None,
    model: Optional[str] = None,
) -> AnnotatedTranscript:
    """Stage A: upload audio to Gemini and produce the annotated
    transcript (`gemini_annotate_v1`) — the artifact Stage B judges from.

    No rubric, no SOP, no grounding: the annotator observes, it never
    scores. One internal retry (bumped temperature) on an unusable
    generation; raises after that — the CALLER decides fallback
    semantics (scoring_service treats annotation as an enhancement,
    never a scoring blocker)."""
    client = _get_client()
    mime_type = _get_mime_type(filename)

    with tempfile.NamedTemporaryFile(suffix=Path(filename).suffix, delete=False) as tmp:
        tmp.write(audio_bytes)
        tmp_path = tmp.name

    uploaded = None
    try:
        uploaded = client.files.upload(
            file=tmp_path,
            config=types.UploadFileConfig(mime_type=mime_type, display_name=filename),
        )
        prompt = build_annotator_prompt(
            transcript_text=transcript_text,
            moments_display=moments_display,
        )
        resolved_model = model or annotator_model()
        last_exc: Exception | None = None
        for temperature in _ANNOTATE_ATTEMPT_TEMPS:
            try:
                return await _annotate_once(
                    client, uploaded, mime_type, prompt, resolved_model, temperature
                )
            except Exception as exc:  # noqa: BLE001 — one retry, then surface
                last_exc = exc
                logger.warning(
                    "annotate attempt at temp=%s failed (%s) — %s",
                    temperature, exc,
                    "retrying with bumped temperature"
                    if temperature != _ANNOTATE_ATTEMPT_TEMPS[-1] else "giving up",
                )
        raise last_exc  # type: ignore[misc]  # loop always ran at least once
    finally:
        Path(tmp_path).unlink(missing_ok=True)
        if uploaded is not None:
            try:
                client.files.delete(name=uploaded.name)
            except Exception:
                pass

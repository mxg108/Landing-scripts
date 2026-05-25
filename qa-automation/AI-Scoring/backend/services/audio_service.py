"""Gemini audio upload and scoring."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from google import genai
from google.genai import types

from backend.prompts.qa_scoring_prompt import build_prompt, build_system_prompt
from backend.models.scorecard import Scorecard

if TYPE_CHECKING:
    from backend.config.team_config import TeamConfig

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
    moments_text: str = "",
    sop_title: str = "",
    sop_content: str = "",
    agent_name: str = "",
    extra_notes: str = "",
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
            moments_text=moments_text,
            sop_title=sop_title,
            sop_content=sop_content,
            agent_name=agent_name,
            extra_notes=extra_notes,
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

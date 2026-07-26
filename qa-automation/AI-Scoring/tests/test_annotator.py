"""Stage-A annotator — TwoStageScoringDesign P2.

Pins: the additive gemini_annotate_v1 schema extension, the annotator
prompt's doctrine lines (audio-SOT, non-English re-transcription,
observational holds), the renderer's interleave + observational
labeling, the SCORING_PIPELINE gate, annotate_audio's parse/cleanup
path, and the eval_store persistence + models_used audio-leg stamp.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from backend.models.formula import AnnotatedTranscript, HoldSegment, TranscriptTurn
from backend.prompts.annotator_prompt import (
    build_annotator_prompt,
    build_annotator_system_prompt,
)
from backend.services.annotation_render import render_annotated_transcript
from backend.services.scoring_service import scoring_pipeline


# ---------------------------------------------------------------------------
# Schema extension (§3 — additive on §8.2)
# ---------------------------------------------------------------------------

def _turn(**overrides):
    base = dict(speaker="agent", text="Buenos días", emotion="neutral_friendly",
                paraphrase_intent="greeting", pace_marker="normal",
                interruption=False, start_ms=1200, end_ms=4800)
    base.update(overrides)
    return TranscriptTurn(**base)


def test_gemini_annotate_v1_shape_roundtrips():
    annotation = AnnotatedTranscript(
        schema_version="gemini_annotate_v1",
        language_detected="es",
        turns=[_turn()],
        holds=[HoldSegment(start_ms=182_000, end_ms=245_000, kind="hold_music",
                           note="announced")],
        call_observations=["background noise on caller side"],
    )
    dumped = annotation.model_dump()
    assert AnnotatedTranscript.model_validate(dumped) == annotation


def test_legacy_qwen_shape_still_validates():
    """The original §8.2 shape (no holds/call_observations) must keep
    validating — schema_version exists so variants coexist."""
    legacy = {
        "schema_version": "qwen2_audio_v1",
        "language_detected": "en",
        "turns": [_turn().model_dump()],
    }
    annotation = AnnotatedTranscript.model_validate(legacy)
    assert annotation.holds == [] and annotation.call_observations == []


def test_hold_kind_is_closed_vocabulary():
    with pytest.raises(Exception):
        HoldSegment(start_ms=0, end_ms=1, kind="coffee_break")


# ---------------------------------------------------------------------------
# Annotator prompt doctrine (§3.1 / §3.2)
# ---------------------------------------------------------------------------

def test_prompt_carries_audio_sot_and_language_rule():
    prompt = build_annotator_prompt(transcript_text="hello", moments_display=[])
    assert "gemini_annotate_v1" in prompt
    assert "AUDIO IS THE SOURCE OF TRUTH" in prompt
    assert "NOT in English" in prompt          # re-transcribe rule
    assert "do NOT translate" in prompt
    system = build_annotator_system_prompt()
    assert "never score" in system


def test_prompt_subordinates_transcript_and_markers_as_hints():
    prompt = build_annotator_prompt(
        transcript_text="the words",
        moments_display=[{"timestamp": "01:00", "type": "hold"}],
    )
    assert "hint — the audio overrules it" in prompt
    assert "hints, machine-detected" in prompt
    # Both omitted cleanly when absent
    bare = build_annotator_prompt(transcript_text="", moments_display=None)
    assert "REFERENCE TRANSCRIPT" not in bare
    assert "SIGNAL MARKERS" not in bare


# ---------------------------------------------------------------------------
# Renderer (§4) — the Stage-B input format
# ---------------------------------------------------------------------------

def test_render_interleaves_holds_and_labels_them_observational():
    annotation = AnnotatedTranscript(
        schema_version="gemini_annotate_v1",
        language_detected="en",
        turns=[
            _turn(text="one", start_ms=0, end_ms=1000),
            _turn(text="two", speaker="caller", emotion="frustrated",
                  interruption=True, start_ms=70_000, end_ms=75_000),
        ],
        holds=[HoldSegment(start_ms=10_000, end_ms=63_000, kind="dead_air")],
        call_observations=["poor audio quality"],
    )
    rendered = render_annotated_transcript(annotation)
    lines = rendered.splitlines()
    # Hold sits between the turns (10s is after turn one, before turn two)
    hold_idx = next(i for i, l in enumerate(lines) if "HOLD" in l)
    one_idx = next(i for i, l in enumerate(lines) if '"one"' in l)
    two_idx = next(i for i, l in enumerate(lines) if '"two"' in l)
    assert one_idx < hold_idx < two_idx
    # §3.1 wording rule enforced at the render boundary
    assert "observational — not system-verified" in lines[hold_idx]
    assert "~53s" in lines[hold_idx]
    assert "[interrupts]" in lines[two_idx] and "frustrated" in lines[two_idx]
    assert rendered.endswith("- poor audio quality")


def test_render_without_holds_or_observations_is_clean():
    annotation = AnnotatedTranscript(
        schema_version="gemini_annotate_v1", turns=[_turn()],
    )
    rendered = render_annotated_transcript(annotation)
    assert "HOLD" not in rendered and "CALL-LEVEL" not in rendered
    assert "Language detected: unknown" in rendered


# ---------------------------------------------------------------------------
# SCORING_PIPELINE gate (§6)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("", "single"),
    ("single", "single"),
    ("annotate_only", "annotate_only"),
    ("two_stage_shadow", "two_stage_shadow"),
    ("two_stage", "two_stage"),
    ("TWO_STAGE", "two_stage"),
    ("bogus", "single"),
])
def test_scoring_pipeline_env_collapse(monkeypatch, raw, expected):
    if raw:
        monkeypatch.setenv("SCORING_PIPELINE", raw)
    else:
        monkeypatch.delenv("SCORING_PIPELINE", raising=False)
    assert scoring_pipeline() == expected


# ---------------------------------------------------------------------------
# annotate_audio — parse + cleanup with a fake Gemini client
# ---------------------------------------------------------------------------

def _fake_genai_client(payload_text):
    class _Uploaded:
        uri = "files/fake"
        name = "files/fake"

    class _Files:
        def __init__(self):
            self.deleted = []

        def upload(self, *, file, config):
            return _Uploaded()

        def delete(self, *, name):
            self.deleted.append(name)

    class _AioModels:
        async def generate_content(self, *, model, contents, config):
            class _Resp:
                text = payload_text
            return _Resp()

    class _Aio:
        models = _AioModels()

    class _Client:
        def __init__(self):
            self.files = _Files()
            self.aio = _Aio()

    return _Client()


def _annotation_json():
    return json.dumps({
        "schema_version": "gemini_annotate_v1",
        "language_detected": "en",
        "turns": [_turn().model_dump()],
        "holds": [],
        "call_observations": [],
    })


def test_annotate_audio_parses_and_cleans_up(monkeypatch):
    import backend.services.audio_service as audio_service

    client = _fake_genai_client(f"```json\n{_annotation_json()}\n```")
    monkeypatch.setattr(audio_service, "_get_client", lambda: client)
    annotation = asyncio.run(audio_service.annotate_audio(
        b"bytes", "call.mp3", transcript_text="hi", moments_display=[],
    ))
    assert annotation.schema_version == "gemini_annotate_v1"
    assert client.files.deleted == ["files/fake"]   # upload cleaned up


def test_annotate_audio_invalid_json_raises(monkeypatch):
    import backend.services.audio_service as audio_service

    client = _fake_genai_client("I am not JSON")
    monkeypatch.setattr(audio_service, "_get_client", lambda: client)
    with pytest.raises(ValueError):
        asyncio.run(audio_service.annotate_audio(b"bytes", "call.mp3"))


def test_annotator_model_env_override(monkeypatch):
    from backend.services.audio_service import annotator_model
    monkeypatch.delenv("ANNOTATOR_MODEL", raising=False)
    assert annotator_model() == "gemini-2.5-flash"
    monkeypatch.setenv("ANNOTATOR_MODEL", "gemini-2.5-pro")
    assert annotator_model() == "gemini-2.5-pro"

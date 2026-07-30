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


def test_response_schema_pinned_to_pydantic_contract():
    """The hand-authored Gemini response_schema (OpenAPI subset — no
    additionalProperties) must track the pydantic models field-for-field
    so constrained decoding and post-hoc validation can't drift."""
    from backend.prompts.annotator_prompt import ANNOTATOR_RESPONSE_SCHEMA as s

    assert set(s["properties"]) == set(AnnotatedTranscript.model_fields)
    turn = s["properties"]["turns"]["items"]
    assert set(turn["properties"]) == set(TranscriptTurn.model_fields)
    hold = s["properties"]["holds"]["items"]
    assert set(hold["properties"]) == set(HoldSegment.model_fields)
    assert hold["properties"]["kind"]["enum"] == [
        "hold_music", "dead_air", "mute_suspected",
    ]
    # The field Gemini's schema dialect rejects must never reappear.
    assert "additionalProperties" not in json.dumps(s)


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
    # Anti-loop rule: stutter runs collapse instead of being spelled out
    assert "COLLAPSE filler and stutter runs" in prompt
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

def _fake_genai_client(*payload_texts, finish_reason=None):
    """One payload per generate attempt (retry consumes the next);
    the last repeats if attempts exceed payloads. Records the
    temperature + response config of each attempt. *finish_reason*
    stamps every response's candidate (e.g. "FinishReason.MAX_TOKENS")."""
    payloads = list(payload_texts)

    class _Uploaded:
        uri = "files/fake"
        name = "files/fake"

    class _AioFiles:
        def __init__(self):
            self.deleted = []

        async def upload(self, *, file, config):
            return _Uploaded()

        async def delete(self, *, name):
            self.deleted.append(name)

    class _AioModels:
        def __init__(self):
            self.attempts = []

        async def generate_content(self, *, model, contents, config):
            self.attempts.append(config)
            text = payloads[min(len(self.attempts) - 1, len(payloads) - 1)]
            class _Cand:
                pass
            _Cand.finish_reason = finish_reason or "FinishReason.STOP"
            class _Resp:
                pass
            _Resp.text = text
            _Resp.candidates = [_Cand()]
            return _Resp()

    class _Aio:
        def __init__(self):
            self.models = _AioModels()
            self.files = _AioFiles()

    class _Client:
        def __init__(self):
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
    assert client.aio.files.deleted == ["files/fake"]   # upload cleaned up
    # Constrained decoding requested (the bilingual-call fix)
    (config,) = client.aio.models.attempts
    assert config.response_mime_type == "application/json"
    assert config.temperature == 0.0
    # Bounded thinking (the MAX_TOKENS fix: thoughts spend from the
    # output budget, and the budget belongs to the transcript)
    assert config.thinking_config.thinking_budget == 4096


def test_annotate_audio_retries_once_with_bumped_temperature(monkeypatch):
    """The observed failure class: temp-0 decoding re-serving the same
    degenerate sample. First attempt bad → one retry at bumped temp."""
    import backend.services.audio_service as audio_service

    client = _fake_genai_client("NOT JSON", _annotation_json())
    monkeypatch.setattr(audio_service, "_get_client", lambda: client)
    annotation = asyncio.run(audio_service.annotate_audio(b"bytes", "call.mp3"))
    assert annotation.schema_version == "gemini_annotate_v1"
    first, second = client.aio.models.attempts
    assert first.temperature == 0.0
    assert second.temperature == 0.2
    assert client.aio.files.deleted == ["files/fake"]   # cleanup after retries too


def test_annotate_audio_invalid_json_raises_after_both_attempts(monkeypatch):
    import backend.services.audio_service as audio_service

    client = _fake_genai_client("I am not JSON")
    monkeypatch.setattr(audio_service, "_get_client", lambda: client)
    with pytest.raises(ValueError, match="annotation unusable"):
        asyncio.run(audio_service.annotate_audio(b"bytes", "call.mp3"))
    assert len(client.aio.models.attempts) == 2    # retried, then surfaced


def test_annotate_audio_max_tokens_names_truncation(monkeypatch):
    """A MAX_TOKENS candidate with NOTHING salvageable must raise an
    error naming the real problem (budget exhaustion), not a JSON
    syntax artifact."""
    import backend.services.audio_service as audio_service

    client = _fake_genai_client(
        '{"schema_version": "gemini_annotate_v1", "turns": [',   # no complete turn
        finish_reason="FinishReason.MAX_TOKENS",
    )
    monkeypatch.setattr(audio_service, "_get_client", lambda: client)
    with pytest.raises(ValueError, match="output budget exhausted"):
        asyncio.run(audio_service.annotate_audio(b"bytes", "call.mp3"))
    assert len(client.aio.models.attempts) == 2    # still retried once


_TRUNCATED_MID_LOOP = (
    '{"schema_version": "gemini_annotate_v1", "language_detected": "es", '
    '"turns": ['
    '{"speaker": "agent", "text": "Hola, buenas tardes.", "start_ms": 200, '
    '"end_ms": 3660}, '
    '{"speaker": "caller", "text": "uh uh uh uh uh uh uh uh uh uh uh uh'
)


def test_salvage_cuts_to_last_complete_turn():
    """The observed failure: clean turns, then a repetition loop inside
    one text field until the budget dies. Salvage keeps the clean turns,
    structurally discards the looping tail, and stamps the truncation."""
    from backend.services.audio_service import _salvage_truncated_annotation

    salvaged = _salvage_truncated_annotation(_TRUNCATED_MID_LOOP)
    assert salvaged is not None
    assert len(salvaged.turns) == 1
    assert salvaged.turns[0].text == "Hola, buenas tardes."
    assert "uh uh" not in json.dumps(salvaged.model_dump())
    assert any("ANNOTATION TRUNCATED" in o for o in salvaged.call_observations)


def test_salvage_returns_none_when_nothing_usable():
    from backend.services.audio_service import _salvage_truncated_annotation

    assert _salvage_truncated_annotation("") is None
    assert _salvage_truncated_annotation("not json at all") is None
    # No complete turn ever closed
    assert _salvage_truncated_annotation(
        '{"schema_version": "gemini_annotate_v1", "turns": [{"speaker": "ag'
    ) is None


def test_annotate_audio_salvages_on_final_attempt_only(monkeypatch):
    """Attempt 1 truncates → retry (no salvage: a fresh sample might be
    complete). Attempt 2 truncates → salvage the partial artifact."""
    import backend.services.audio_service as audio_service

    client = _fake_genai_client(
        _TRUNCATED_MID_LOOP,
        finish_reason="FinishReason.MAX_TOKENS",
    )
    monkeypatch.setattr(audio_service, "_get_client", lambda: client)
    annotation = asyncio.run(audio_service.annotate_audio(b"bytes", "call.mp3"))
    assert len(client.aio.models.attempts) == 2    # salvage came second
    assert len(annotation.turns) == 1
    assert any("ANNOTATION TRUNCATED" in o for o in annotation.call_observations)


def test_annotator_model_env_override(monkeypatch):
    from backend.services.audio_service import annotator_model
    monkeypatch.delenv("ANNOTATOR_MODEL", raising=False)
    assert annotator_model() == "gemini-2.5-flash"
    monkeypatch.setenv("ANNOTATOR_MODEL", "gemini-2.5-pro")
    assert annotator_model() == "gemini-2.5-pro"


def test_annotator_thinking_budget_env_override(monkeypatch):
    from backend.services.audio_service import annotator_thinking_budget
    monkeypatch.delenv("ANNOTATOR_THINKING_BUDGET", raising=False)
    assert annotator_thinking_budget() == 4096
    monkeypatch.setenv("ANNOTATOR_THINKING_BUDGET", "8192")
    assert annotator_thinking_budget() == 8192
    monkeypatch.setenv("ANNOTATOR_THINKING_BUDGET", "not-a-number")
    assert annotator_thinking_budget() == 4096


def test_annotate_audio_fails_fast_on_deterministic_400(monkeypatch):
    """A 400 INVALID_ARGUMENT (model rejects audio input, unsupported
    knob) is deterministic — retrying the identical request wastes a
    call. One attempt, immediate surface."""
    from google.genai import errors as genai_errors
    import backend.services.audio_service as audio_service

    attempts = []

    class _AioModels:
        async def generate_content(self, **kwargs):
            attempts.append(kwargs)
            raise genai_errors.ClientError(
                400, {"error": {"message": "Request contains an invalid argument."}}
            )

    class _AioFiles:
        async def upload(self, *, file, config):
            class _Up:
                uri = "files/fake"
                name = "files/fake"
            return _Up()

        async def delete(self, *, name):
            pass

    class _Aio:
        models = _AioModels()
        files = _AioFiles()

    class _Client:
        aio = _Aio()

    monkeypatch.setattr(audio_service, "_get_client", lambda: _Client())
    with pytest.raises(genai_errors.ClientError):
        asyncio.run(audio_service.annotate_audio(b"bytes", "call.mp3"))
    assert len(attempts) == 1    # no retry on a deterministic rejection

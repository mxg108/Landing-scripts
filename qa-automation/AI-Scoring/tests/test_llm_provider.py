"""llm/ provider seam — ModelProviderDesign P1 / TwoStageScoringDesign §4.

Pins: factory resolution (env-keyed, house pattern), the Gemini
provider's knob mapping (pure-refactor parity with the pre-seam
progression client code), the Anthropic provider's request shape
(no sampling params, adaptive thinking, structured-output opt-in),
and the vendor-import grep gates.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from backend.services.llm.anthropic import AnthropicTextProvider
from backend.services.llm.factory import resolve_stage
from backend.services.llm.gemini import GeminiTextProvider
from backend.services.llm.provider import LlmProviderError, LlmResult
from tests.conftest import load_test_config

_BACKEND = Path(__file__).resolve().parent.parent / "backend"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeGenaiClient:
    """Mimics google-genai's async surface; records the call."""

    def __init__(self):
        self.calls = []
        outer = self

        class _AioModels:
            async def generate_content(self, *, model, contents, config):
                outer.calls.append(
                    {"model": model, "contents": contents, "config": config}
                )
                class _Resp:
                    text = '{"ok": true}'
                return _Resp()

        class _Aio:
            models = _AioModels()

        self.aio = _Aio()


class FakeAnthropicClient:
    """Mimics the anthropic SDK's messages.create; records the call."""

    def __init__(self, stop_reason="end_turn", text="hello"):
        self.calls = []
        outer = self

        class _Block:
            type = "text"
        _Block.text = text

        class _Resp:
            model = "claude-sonnet-5"
            content = [_Block()] if text else []
        _Resp.stop_reason = stop_reason

        class _Messages:
            async def create(self, **kwargs):
                outer.calls.append(kwargs)
                return _Resp()

        self.messages = _Messages()


# ---------------------------------------------------------------------------
# Factory resolution
# ---------------------------------------------------------------------------

def test_factory_defaults_to_gemini_with_team_knobs(monkeypatch):
    monkeypatch.delenv("PROGRESSION_MODEL_PROVIDER", raising=False)
    config = load_test_config("sales_lite")
    stage = resolve_stage("progression", config)
    assert isinstance(stage.provider, GeminiTextProvider)
    assert stage.model == config.gemini.progression_model
    assert stage.max_output_tokens == config.gemini.progression_max_output_tokens


def test_factory_env_flips_to_anthropic(monkeypatch):
    monkeypatch.setenv("PROGRESSION_MODEL_PROVIDER", "anthropic")
    monkeypatch.delenv("PROGRESSION_ANTHROPIC_MODEL", raising=False)
    stage = resolve_stage("progression", load_test_config("sales_lite"))
    assert isinstance(stage.provider, AnthropicTextProvider)
    assert stage.model == "claude-sonnet-5"     # §10 starting tier


def test_factory_unknown_provider_collapses_to_gemini(monkeypatch):
    monkeypatch.setenv("PROGRESSION_MODEL_PROVIDER", "openai")
    stage = resolve_stage("progression", load_test_config("sales_lite"))
    assert isinstance(stage.provider, GeminiTextProvider)


def test_factory_rejects_unknown_stage():
    with pytest.raises(ValueError):
        resolve_stage("annotation", load_test_config("sales_lite"))


# ---------------------------------------------------------------------------
# Gemini provider — pure-refactor parity with the pre-seam client code
# ---------------------------------------------------------------------------

def test_gemini_maps_model_temperature_and_tokens():
    fake = FakeGenaiClient()
    provider = GeminiTextProvider(temperature=0.3, client=fake)
    result = asyncio.run(provider.generate(
        "the prompt", model="gemini-2.5-flash", max_output_tokens=4096,
    ))
    assert result == LlmResult(
        text='{"ok": true}', provider="gemini", model="gemini-2.5-flash"
    )
    (call,) = fake.calls
    assert call["contents"] == "the prompt"
    assert call["config"].temperature == 0.3
    assert call["config"].max_output_tokens == 4096


def test_gemini_missing_key_keeps_legacy_error(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    provider = GeminiTextProvider()
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY not set"):
        asyncio.run(provider.generate(
            "x", model="m", max_output_tokens=1,
        ))


def test_gemini_structured_output_raises_not_silently_ignores():
    provider = GeminiTextProvider(client=FakeGenaiClient())
    with pytest.raises(LlmProviderError):
        asyncio.run(provider.generate(
            "x", model="m", max_output_tokens=1, json_schema={"type": "object"},
        ))


# ---------------------------------------------------------------------------
# Anthropic provider — request-shape doctrine
# ---------------------------------------------------------------------------

def test_anthropic_request_shape_no_sampling_adaptive_thinking():
    fake = FakeAnthropicClient()
    provider = AnthropicTextProvider(client=fake)
    result = asyncio.run(provider.generate(
        "judge this", model="claude-sonnet-5", max_output_tokens=8192,
    ))
    assert result.provider == "anthropic"
    assert result.text == "hello"
    (call,) = fake.calls
    assert call["model"] == "claude-sonnet-5"
    assert call["max_tokens"] == 8192
    assert call["thinking"] == {"type": "adaptive"}
    assert call["messages"] == [{"role": "user", "content": "judge this"}]
    # Current Claude models reject sampling params — none may be sent.
    assert "temperature" not in call and "top_p" not in call
    assert "output_config" not in call    # only with json_schema


def test_anthropic_json_schema_sets_structured_output():
    fake = FakeAnthropicClient(text='{"score": 5}')
    provider = AnthropicTextProvider(client=fake)
    asyncio.run(provider.generate(
        "x", model="claude-sonnet-5", max_output_tokens=100,
        json_schema={"type": "object"},
    ))
    (call,) = fake.calls
    assert call["output_config"] == {
        "format": {"type": "json_schema", "schema": {"type": "object"}}
    }


def test_anthropic_refusal_raises():
    provider = AnthropicTextProvider(
        client=FakeAnthropicClient(stop_reason="refusal")
    )
    with pytest.raises(LlmProviderError, match="refused"):
        asyncio.run(provider.generate(
            "x", model="claude-sonnet-5", max_output_tokens=1,
        ))


def test_anthropic_missing_key_raises_cleanly(monkeypatch):
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY_LANDING",
                "ANTHROPIC_API_KEY_PERSONAL"):
        monkeypatch.delenv(var, raising=False)
    provider = AnthropicTextProvider()
    with pytest.raises(LlmProviderError, match="Anthropic key"):
        asyncio.run(provider.generate(
            "x", model="claude-sonnet-5", max_output_tokens=1,
        ))


# ---------------------------------------------------------------------------
# Vendor-import grep gates (same guard pattern as the Notion gate)
# ---------------------------------------------------------------------------

def _py_files():
    for path in _BACKEND.rglob("*.py"):
        if "__pycache__" not in path.parts:
            yield path


def test_anthropic_sdk_imports_only_inside_llm_seam():
    allowed = {_BACKEND / "services" / "llm" / "anthropic.py"}
    offenders = [
        str(p.relative_to(_BACKEND))
        for p in _py_files()
        if p not in allowed
        and ("from anthropic" in p.read_text() or "import anthropic" in p.read_text())
    ]
    assert not offenders, (
        f"anthropic SDK usage outside the llm/ seam: {offenders} "
        "(ModelProviderDesign: anthropic.py is the only Claude-shaped module)"
    )


def test_genai_text_imports_only_in_seam_and_audio_leg():
    """google-genai belongs to llm/gemini.py (text seam) and
    audio_service.py (the Gemini-native Stage-A audio leg)."""
    allowed = {
        _BACKEND / "services" / "llm" / "gemini.py",
        _BACKEND / "services" / "audio_service.py",
    }
    offenders = [
        str(p.relative_to(_BACKEND))
        for p in _py_files()
        if p not in allowed and "from google import genai" in p.read_text()
    ]
    assert not offenders, (
        f"google-genai usage outside llm/gemini.py + audio_service.py: {offenders}"
    )

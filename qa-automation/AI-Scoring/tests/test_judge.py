"""Stage-B judge — TwoStageScoringDesign P3.

Pins: factory "scoring" stage resolution, the system-prompt channel on
the llm contract, the judge prompt's doctrine (annotation authoritative,
observational holds, truncation handling, agent-name rule kept in sync
with the single-stage prompt), judge_service parsing, the shadow-judge
stamp, and eval_store's text-leg/fallback/shadow provenance.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from backend.models.formula import AnnotatedTranscript, TranscriptTurn
from backend.prompts.judge_prompt import (
    JUDGE_GENERAL_INSTRUCTIONS,
    build_judge_prompt,
    build_judge_system_prompt,
)
from backend.prompts.qa_scoring_prompt import LANDING_GENERAL_INSTRUCTIONS
from backend.services.llm.anthropic import AnthropicTextProvider
from backend.services.llm.factory import resolve_stage
from backend.services.llm.gemini import GeminiTextProvider
from backend.services.llm.provider import LlmResult, TextModelProvider
from tests.conftest import load_test_config, make_gemini_scoring_json


@pytest.fixture(scope="module")
def config():
    return load_test_config("sales_lite")


def _annotation():
    return AnnotatedTranscript(
        schema_version="gemini_annotate_v1",
        language_detected="en",
        turns=[TranscriptTurn(speaker="agent", text="Hello, this is Sam.",
                              emotion="warm", start_ms=0, end_ms=2000)],
    )


# ---------------------------------------------------------------------------
# Factory — scoring stage
# ---------------------------------------------------------------------------

def test_factory_scoring_defaults_to_gemini_scoring_knobs(monkeypatch, config):
    monkeypatch.delenv("SCORING_MODEL_PROVIDER", raising=False)
    stage = resolve_stage("scoring", config)
    assert isinstance(stage.provider, GeminiTextProvider)
    assert stage.model == config.gemini.scoring_model
    assert stage.max_output_tokens == config.gemini.scoring_max_output_tokens


def test_factory_scoring_env_flips_to_anthropic(monkeypatch, config):
    monkeypatch.setenv("SCORING_MODEL_PROVIDER", "anthropic")
    monkeypatch.delenv("SCORING_ANTHROPIC_MODEL", raising=False)
    stage = resolve_stage("scoring", config)
    assert isinstance(stage.provider, AnthropicTextProvider)
    assert stage.model == "claude-sonnet-5"    # §10.2 starting tier
    monkeypatch.setenv("SCORING_ANTHROPIC_MODEL", "claude-haiku-4-5")
    assert resolve_stage("scoring", config).model == "claude-haiku-4-5"


def test_factory_stages_resolve_independently(monkeypatch, config):
    """Flipping the judge must not flip progression, and vice versa."""
    monkeypatch.setenv("SCORING_MODEL_PROVIDER", "anthropic")
    monkeypatch.delenv("PROGRESSION_MODEL_PROVIDER", raising=False)
    assert isinstance(resolve_stage("scoring", config).provider,
                      AnthropicTextProvider)
    assert isinstance(resolve_stage("progression", config).provider,
                      GeminiTextProvider)


# ---------------------------------------------------------------------------
# Provider system-channel
# ---------------------------------------------------------------------------

def test_gemini_provider_maps_system_instruction():
    from tests.test_llm_provider import FakeGenaiClient

    fake = FakeGenaiClient()
    provider = GeminiTextProvider(client=fake)
    asyncio.run(provider.generate(
        "p", model="m", max_output_tokens=10, system="be a judge",
    ))
    (call,) = fake.calls
    assert call["config"].system_instruction == "be a judge"


def test_anthropic_provider_maps_system_param():
    from tests.test_llm_provider import FakeAnthropicClient

    fake = FakeAnthropicClient()
    provider = AnthropicTextProvider(client=fake)
    asyncio.run(provider.generate(
        "p", model="claude-sonnet-5", max_output_tokens=10, system="be a judge",
    ))
    (call,) = fake.calls
    assert call["system"] == "be a judge"


# ---------------------------------------------------------------------------
# Judge prompt doctrine
# ---------------------------------------------------------------------------

def test_judge_prompt_carries_annotation_doctrine(config):
    prompt = build_judge_prompt(config, "RENDERED-RECORD",
                                call_context_text="=== CALL CONTEXT ===")
    assert "ANNOTATED CALL RECORD (authoritative)" in prompt
    assert "RENDERED-RECORD" in prompt
    assert "observational — not system-verified" in prompt
    assert "ANNOTATION TRUNCATED" in prompt
    assert "No audio is attached" in prompt
    # Rubric + output schema reused from the single-stage prompt
    assert "=== SCORING RUBRIC ===" in prompt
    # Grounding block rides ahead of the record, same as single-stage
    assert prompt.index("CALL CONTEXT") < prompt.index("ANNOTATED CALL RECORD")


def test_judge_prompt_sop_missing_path(config):
    prompt = build_judge_prompt(config, "X")
    assert "SOP" in prompt          # missing-note present
    assert "DO NOT mention a lack of SOP" in prompt


def test_judge_system_prompt_agent_name_rule_in_sync(config):
    """The agent-name rule is duplicated (judge swaps only the SOT
    bullet) — this gate keeps the shared sentences identical."""
    shared = (
        "NOT penalize the agent for the specific name used in the greeting."
    )
    assert shared in LANDING_GENERAL_INSTRUCTIONS
    assert shared in JUDGE_GENERAL_INSTRUCTIONS
    system = build_judge_system_prompt(config)
    assert shared in system
    # The judge must NOT claim audio is its source of truth.
    assert "the audio is authoritative" not in system
    assert "ANNOTATED CALL RECORD" in system


# ---------------------------------------------------------------------------
# judge_service — generation + parse
# ---------------------------------------------------------------------------

class FakeJudgeProvider(TextModelProvider):
    name = "fake"

    def __init__(self, text):
        self._text = text
        self.calls = []

    async def generate(self, prompt, *, model, max_output_tokens,
                       json_schema=None, system=None):
        self.calls.append({"prompt": prompt, "model": model, "system": system})
        return LlmResult(text=self._text, provider="anthropic", model=model)


def test_score_annotation_parses_and_returns_provenance(monkeypatch, config):
    from backend.services import judge_service
    from backend.services.llm.factory import StageModel

    raw = json.dumps(make_gemini_scoring_json(config))
    fake = FakeJudgeProvider(f"```json\n{raw}\n```")
    monkeypatch.setattr(
        judge_service, "resolve_stage",
        lambda stage, cfg: StageModel(provider=fake, model="claude-sonnet-5",
                                      max_output_tokens=8192),
    )
    scorecard, result = asyncio.run(judge_service.score_annotation(
        _annotation(), config, sop_title="T", sop_content="C",
        agent_name="Sam", call_context_text="=== CALL CONTEXT ===",
    ))
    assert scorecard.sections                     # validated Scorecard
    assert result.provider == "anthropic"
    (call,) = fake.calls
    assert "ANNOTATED CALL RECORD" in call["prompt"]
    assert '"Hello, this is Sam."' in call["prompt"]   # rendered record
    assert call["system"] and "ANNOTATED CALL RECORD" in call["system"]


def test_score_annotation_surfaces_parse_failure(monkeypatch, config):
    from backend.services import judge_service
    from backend.services.llm.factory import StageModel

    fake = FakeJudgeProvider("not json")
    monkeypatch.setattr(
        judge_service, "resolve_stage",
        lambda stage, cfg: StageModel(provider=fake, model="m",
                                      max_output_tokens=10),
    )
    with pytest.raises(ValueError):
        asyncio.run(judge_service.score_annotation(_annotation(), config))


# ---------------------------------------------------------------------------
# Shadow judge stamp
# ---------------------------------------------------------------------------

def test_run_shadow_judge_stamps_compare(monkeypatch, config):
    from backend.models.scorecard import Scorecard
    from backend.services import scoring_service

    primary = Scorecard(**make_gemini_scoring_json(config, score_seed=4))
    shadow = Scorecard(**make_gemini_scoring_json(config, score_seed=5))

    async def fake_judge(annotation, cfg, **kw):
        return shadow, LlmResult(text="", provider="anthropic",
                                 model="claude-sonnet-5")

    monkeypatch.setattr(scoring_service, "score_annotation", fake_judge)
    stamp = asyncio.run(scoring_service._run_shadow_judge(
        _annotation(), config, primary,
        sop_data={"sop_title": "", "sop_content": ""},
        agent_name="Sam", extra_notes="", call_context_text="",
        call_id="C1",
    ))
    assert stamp["scorer_provider"] == "anthropic"
    assert stamp["scorer_model"] == "claude-sonnet-5"
    # seed 4 vs 5 → every numeric section disagrees; yn sections agree
    numeric_ids = {s["id"] for s in make_gemini_scoring_json(config)["sections"]
                   if s["score_type"] == "numeric"}
    assert set(stamp["mismatched_section_ids"]) == numeric_ids
    assert set(stamp["sections"]) == {
        s["id"] for s in make_gemini_scoring_json(config)["sections"]
    }


def test_run_shadow_judge_never_raises(monkeypatch, config):
    from backend.models.scorecard import Scorecard
    from backend.services import scoring_service

    async def boom(annotation, cfg, **kw):
        raise RuntimeError("judge exploded")

    monkeypatch.setattr(scoring_service, "score_annotation", boom)
    stamp = asyncio.run(scoring_service._run_shadow_judge(
        _annotation(), config,
        Scorecard(**make_gemini_scoring_json(config)),
        sop_data={"sop_title": "", "sop_content": ""},
        agent_name="", extra_notes="", call_context_text="", call_id="C1",
    ))
    assert "judge exploded" in stamp["error"]
    assert "elapsed_s" in stamp


# ---------------------------------------------------------------------------
# eval_store provenance
# ---------------------------------------------------------------------------

def test_eval_store_stamps_judge_provenance():
    from tests.test_eval_store import _scorecard
    from backend.services.eval_store import build_draft_row

    ms_config = load_test_config("member_support")
    sc = _scorecard(
        ms_config,
        scorer_provider="anthropic",
        model="claude-sonnet-5",
        annotated_transcript={"schema_version": "gemini_annotate_v1",
                              "turns": []},
        annotator_model="gemini-2.5-flash",
    )
    row = build_draft_row(sc, ms_config)
    models_used = json.loads(row["models_used"])
    assert models_used["text"] == {"provider": "anthropic",
                                   "model": "claude-sonnet-5"}
    assert row["ai_provider_primary"] == "anthropic"
    assert "fallback" not in models_used


def test_eval_store_stamps_fallback_and_shadow():
    from tests.test_eval_store import _scorecard
    from backend.services.eval_store import build_draft_row

    ms_config = load_test_config("member_support")
    shadow_stamp = {"scorer_provider": "gemini", "sections": {}}
    sc = _scorecard(
        ms_config,
        pipeline_fallback_reason="text_scorer_failed",
        two_stage_shadow=shadow_stamp,
    )
    row = build_draft_row(sc, ms_config)
    models_used = json.loads(row["models_used"])
    assert models_used["fallback"] == {
        "provider": "gemini", "sections": [], "reason": "text_scorer_failed",
    }
    # Fallback rows are authored by single-stage gemini
    assert row["ai_provider_primary"] == "gemini"
    metadata = json.loads(row["dialpad_call_metadata"])
    assert metadata["two_stage_shadow"] == shadow_stamp


def test_eval_store_single_stage_rows_have_no_shadow_key():
    from tests.test_eval_store import _scorecard
    from backend.services.eval_store import build_draft_row

    ms_config = load_test_config("member_support")
    row = build_draft_row(_scorecard(ms_config), ms_config)
    assert "two_stage_shadow" not in json.loads(row["dialpad_call_metadata"])

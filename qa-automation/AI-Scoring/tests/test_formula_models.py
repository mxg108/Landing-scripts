"""Pydantic model tests for the Postgres-owned scoring engine.

Locks the Formula/Rubric/Rule shapes ahead of Wave 2 Phase 2 (rule engine +
compute_overall_score). Uses the shipped MS overall_formula.json + team.json
as the canonical happy-path fixtures.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from backend.models.formula import (
    AnnotatedTranscript,
    AssessmentSection,
    EvaluationSection,
    FallbackInfo,
    Formula,
    FormulaRubricMismatchError,
    ModelsUsed,
    RecordingUrls,
    Rubric,
    RubricSection,
    WEIGHTED_SUM,
    validate_formula_against_rubric,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_MS_TEAM_JSON = _REPO_ROOT / "backend" / "config" / "teams" / "member_support.json"
_MS_FORMULA_JSON = _REPO_ROOT / "backend" / "config" / "scoring" / "member_support" / "overall_formula.json"


@pytest.fixture(scope="module")
def ms_formula_raw() -> dict:
    return json.loads(_MS_FORMULA_JSON.read_text())


@pytest.fixture(scope="module")
def ms_team_raw() -> dict:
    return json.loads(_MS_TEAM_JSON.read_text())


@pytest.fixture(scope="module")
def ms_formula(ms_formula_raw) -> Formula:
    return Formula.model_validate(ms_formula_raw)


@pytest.fixture(scope="module")
def ms_rubric(ms_team_raw) -> Rubric:
    return Rubric.model_validate(ms_team_raw["rubric"])


# ---------------------------------------------------------------------------
# Happy-path — ships-with-repo configs load
# ---------------------------------------------------------------------------

class TestMemberSupportShippedConfig:
    """The MS files in the repo must load. If these break, downstream is blocked."""

    def test_formula_loads(self, ms_formula):
        assert ms_formula.formula_id == "member_support_v5"
        assert ms_formula.supersedes == "member_support_v4"
        assert len(ms_formula.sections) == 10
        assert len(ms_formula.rules) == 3

    def test_rubric_loads(self, ms_rubric):
        assert ms_rubric.rubric_version == "member_support_v2"
        assert len(ms_rubric.sections) == 10

    def test_version_link_matches(self, ms_formula, ms_rubric):
        assert ms_formula.rubric_version == ms_rubric.rubric_version

    def test_cross_validation_passes(self, ms_formula, ms_rubric):
        validate_formula_against_rubric(ms_formula, ms_rubric)

    def test_weight_sum_equals_scale_max(self, ms_formula):
        total = sum(s.weight for s in ms_formula.sections)
        assert total == pytest.approx(ms_formula.scale.max)

    def test_ms_na_policy_is_redistribute(self, ms_formula):
        assert ms_formula.normalization.na == "redistribute_per_rules"

    def test_ms_july_v5_gate_is_off(self, ms_formula):
        """July rollout (Director 2026-07-06): human_review_triggers empty —
        every scored call auto-finalizes. The v4 gate returns ~August."""
        assert ms_formula.human_review_triggers == []

    def test_ms_formula_round_trips(self, ms_formula_raw, ms_formula):
        # Preserve alias-keyed dict (`from` → `from_`) via by_alias=True.
        dumped = ms_formula.model_dump(by_alias=True)
        Formula.model_validate(dumped)


class TestSalesCanonicalConfig:
    """Sales §8 canonical config parses cleanly even though the JSON file
    hasn't been regenerated yet (blocked on §10 sign-off)."""

    @pytest.fixture(scope="class")
    def sales_formula(self) -> Formula:
        return Formula.model_validate({
            "formula_id": "sales_v2",
            "rubric_version": "sales_v2",
            "supersedes": None,
            "scale": {"min": 0, "max": 100},
            "normalization": {
                "rating_1_5": {"type": "linear", "input_min": 1, "input_max": 5, "output": [0.0, 1.0]},
                "binary_yn": {"Y": 1.0, "N": 0.0},
                "na": "full_credit",
            },
            "sections": [
                {"key": "greeting",           "label": "Greeting",              "category": "Opening",     "score_type": "binary_yn_na",  "weight": 5.0},
                {"key": "stay_type",          "label": "Personal or COHO Stay", "category": "Opening",     "score_type": "binary_yn_na",  "weight": 4.0},
                {"key": "move_reason",        "label": "Move Reason",           "category": "Discovery",   "score_type": "rating_1_5_na", "weight": 15.0},
                {"key": "landing_intro",      "label": "Landing Intro",         "category": "Discovery",   "score_type": "binary_yn_na",  "weight": 6.0},
                {"key": "timeline_housing",   "label": "Timeline & Housing",    "category": "Discovery",   "score_type": "rating_1_5_na", "weight": 8.0},
                {"key": "landing_guarantee",  "label": "Landing Guarantee",     "category": "Pitch",       "score_type": "rating_1_5_na", "weight": 6.0},
                {"key": "pricing",            "label": "Pricing Breakdown",     "category": "Pitch",       "score_type": "rating_1_5_na", "weight": 8.0},
                {"key": "fit_confirmation",   "label": "Confirmation of Fit",   "category": "Pitch",       "score_type": "rating_1_5_na", "weight": 5.0},
                {"key": "objection_handling", "label": "Objection Handling",    "category": "Objections",  "score_type": "rating_1_5_na", "weight": 8.0},
                {"key": "urgency",            "label": "Urgency",               "category": "Objections",  "score_type": "binary_yn_na",  "weight": 5.0},
                {"key": "follow_up",          "label": "Follow-up",             "category": "Objections",  "score_type": "binary_yn_na",  "weight": 6.0},
                {"key": "potential_booking",  "label": "Potential Booking",     "category": "Post-Call",   "score_type": "binary_yn_na",  "weight": 4.0},
                {"key": "notes_mc",           "label": "Detailed Notes in MC",  "category": "Post-Call",   "score_type": "binary_yn_na",  "weight": 5.0},
                {"key": "contact_shared",     "label": "Contact Shared",        "category": "Post-Call",   "score_type": "binary_yn_na",  "weight": 5.0},
                {"key": "client_experience",  "label": "Client Experience",     "category": "Post-Call",   "score_type": "rating_1_5_na", "weight": 10.0},
            ],
            "rules": [],
            "evaluation_order": [WEIGHTED_SUM],
            "triggers": {},
            "external_signals": {},
            "deprecated_sections": [],
        })

    def test_loads_fifteen_sections(self, sales_formula):
        assert len(sales_formula.sections) == 15

    def test_full_credit_na_policy(self, sales_formula):
        assert sales_formula.normalization.na == "full_credit"

    def test_no_rules_no_triggers(self, sales_formula):
        assert sales_formula.rules == []
        assert sales_formula.human_review_triggers == []

    def test_evaluation_order_is_just_weighted_sum(self, sales_formula):
        assert sales_formula.evaluation_order == [WEIGHTED_SUM]

    def test_weights_sum_to_hundred(self, sales_formula):
        assert sum(s.weight for s in sales_formula.sections) == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# Validator guardrails (Wave 2 Plan §8 delivery risks)
# ---------------------------------------------------------------------------

def _minimal_formula() -> dict:
    """Smallest passing formula — used as a base to mutate for negative tests."""
    return {
        "formula_id": "test_v1",
        "rubric_version": "test_v1",
        "scale": {"min": 0, "max": 100},
        "normalization": {
            "rating_1_5": {"type": "linear", "input_min": 1, "input_max": 5, "output": [0.0, 1.0]},
            "binary_yn": {"Y": 1.0, "N": 0.0},
            "na": "full_credit",
        },
        "sections": [
            {"key": "a", "label": "A", "score_type": "rating_1_5", "weight": 60.0},
            {"key": "b", "label": "B", "score_type": "rating_1_5", "weight": 40.0},
        ],
        "rules": [],
        "evaluation_order": [WEIGHTED_SUM],
        "triggers": {},
        "external_signals": {},
    }


class TestFormulaValidators:

    def test_weight_sum_wrong_raises(self):
        raw = _minimal_formula()
        raw["sections"][0]["weight"] = 50.0  # sum becomes 90
        with pytest.raises(ValidationError, match="section weights sum to"):
            Formula.model_validate(raw)

    def test_missing_na_policy_raises(self):
        raw = _minimal_formula()
        del raw["normalization"]["na"]
        with pytest.raises(ValidationError):
            Formula.model_validate(raw)

    def test_bad_na_policy_raises(self):
        raw = _minimal_formula()
        raw["normalization"]["na"] = "silent_lose"
        with pytest.raises(ValidationError):
            Formula.model_validate(raw)

    def test_evaluation_order_missing_weighted_sum_raises(self):
        raw = _minimal_formula()
        raw["evaluation_order"] = []
        with pytest.raises(ValidationError, match="weighted_sum"):
            Formula.model_validate(raw)

    def test_evaluation_order_unknown_rule_raises(self):
        raw = _minimal_formula()
        raw["evaluation_order"] = [WEIGHTED_SUM, "nonexistent_rule"]
        with pytest.raises(ValidationError, match="unknown tokens"):
            Formula.model_validate(raw)

    def test_evaluation_order_missing_rule_raises(self):
        raw = _minimal_formula()
        raw["rules"] = [{
            "id": "r1", "type": "score_scale", "enabled": True,
            "when": {"section": "a", "equals": "N"},
            "effect": {"multiply": 0.5},
        }]
        raw["evaluation_order"] = [WEIGHTED_SUM]  # r1 defined but not scheduled
        with pytest.raises(ValidationError, match="defined but not scheduled"):
            Formula.model_validate(raw)

    def test_duplicate_section_keys_raise(self):
        raw = _minimal_formula()
        raw["sections"] = [
            {"key": "a", "label": "A", "score_type": "rating_1_5", "weight": 50.0},
            {"key": "a", "label": "A dup", "score_type": "rating_1_5", "weight": 50.0},
        ]
        with pytest.raises(ValidationError, match="duplicate section keys"):
            Formula.model_validate(raw)

    def test_duplicate_rule_ids_raise(self):
        raw = _minimal_formula()
        rule = {
            "id": "same", "type": "score_scale", "enabled": True,
            "when": {"section": "a", "equals": "N"},
            "effect": {"multiply": 0.5},
        }
        raw["rules"] = [rule, dict(rule)]
        raw["evaluation_order"] = ["same", WEIGHTED_SUM]
        with pytest.raises(ValidationError, match="duplicate rule ids"):
            Formula.model_validate(raw)

    def test_human_review_trigger_unknown_section_raises(self):
        raw = _minimal_formula()
        raw["human_review_triggers"] = [{"section_id": "nope", "max_score_to_trigger": 3}]
        with pytest.raises(ValidationError, match="unknown sections"):
            Formula.model_validate(raw)


class TestRuleTypes:
    """One test per rule type — confirms discriminated union routes correctly."""

    def _wrap(self, rule: dict) -> Formula:
        raw = _minimal_formula()
        raw["rules"] = [rule]
        raw["evaluation_order"] = [rule["id"], WEIGHTED_SUM]
        return Formula.model_validate(raw)

    def test_score_override(self):
        f = self._wrap({
            "id": "hz", "type": "score_override", "enabled": False,
            "when": {"section": "a", "equals": "N"},
            "effect": {"set_score": 0},
        })
        assert f.rules[0].type == "score_override"

    def test_weight_transfer_with_amount(self):
        f = self._wrap({
            "id": "wt", "type": "weight_transfer", "enabled": True,
            "when": {"section": "a", "equals": "NA"},
            "effect": {"from": "a", "to": "b", "amount": "all"},
        })
        assert f.rules[0].effect.amount == "all"
        assert f.rules[0].effect.from_ == "a"

    def test_weight_transfer_with_fraction(self):
        f = self._wrap({
            "id": "wt", "type": "weight_transfer", "enabled": True,
            "when": {"signal": "some_signal"},
            "effect": {"from": "a", "to": "b", "fraction": 0.5},
        })
        assert f.rules[0].effect.fraction == 0.5

    def test_weight_transfer_needs_exactly_one_of_amount_or_fraction(self):
        with pytest.raises(ValidationError, match="exactly one of `amount` or `fraction`"):
            self._wrap({
                "id": "wt", "type": "weight_transfer", "enabled": True,
                "when": {"section": "a", "equals": "NA"},
                "effect": {"from": "a", "to": "b"},  # neither
            })

    def test_weight_redistribution(self):
        f = self._wrap({
            "id": "wr", "type": "weight_redistribution", "enabled": True,
            "when": {"section": "a", "equals": "NA"},
            "effect": {"method": "equal_additive", "from": "a", "to": "active_sections"},
        })
        assert f.rules[0].effect.method == "equal_additive"

    def test_score_scale(self):
        f = self._wrap({
            "id": "ss", "type": "score_scale", "enabled": True,
            "when": {"section": "a", "equals": "N"},
            "effect": {"multiply": 0.5},
        })
        assert f.rules[0].effect.multiply == 0.5

    def test_flag(self):
        f = self._wrap({
            "id": "fg", "type": "flag", "enabled": True,
            "when": {"any": [{"section": "a", "frac_lte": 0.5}]},
            "effect": {"emit_event": "review", "route": "supervisor"},
        })
        assert f.rules[0].effect.emit_event == "review"

    def test_na_redistribution(self):
        f = self._wrap({
            "id": "nr", "type": "na_redistribution", "enabled": True,
            "when": {"section": "a", "equals": "NA"},
            "targets": "remaining",
        })
        assert f.rules[0].mode == "proportional"
        assert f.rules[0].targets == "remaining"


# ---------------------------------------------------------------------------
# Rubric — cross-reference guardrails (§3.19)
# ---------------------------------------------------------------------------

class TestRubricValidators:

    def _minimal_rubric(self) -> dict:
        return {
            "rubric_version": "rt_v1",
            "sections": [
                {"id": "a", "history_id": "a", "name": "A", "section_number": 1, "score_type": "numeric"},
                {"id": "b", "history_id": "b", "name": "B", "section_number": 2, "score_type": "yn"},
            ],
            "scoring_prompt": {
                "system_prompt_template": "prompt",
                "confidence_levels_note": "notes",
                "sop_sections": [],
                "long_call_focus_sections": [],
                "audio_dependent_sections": [],
            },
        }

    def test_scoring_prompt_references_unknown_section_raises(self):
        raw = self._minimal_rubric()
        raw["scoring_prompt"]["sop_sections"] = ["nonexistent"]
        with pytest.raises(ValidationError, match="sop_sections references unknown"):
            Rubric.model_validate(raw)

    def test_duplicate_section_ids_raise(self):
        raw = self._minimal_rubric()
        raw["sections"][1]["id"] = "a"
        with pytest.raises(ValidationError, match="duplicate section ids"):
            Rubric.model_validate(raw)

    def test_auto_value_requires_auto_value_score_type(self):
        # `score_type='auto_value'` requires `auto_value` set — inverse of below.
        with pytest.raises(ValidationError, match="requires `auto_value`"):
            RubricSection.model_validate({
                "id": "sr", "history_id": "sr", "name": "screen_recording",
                "section_number": 1, "score_type": "auto_value",
            })


class TestFormulaRubricCrossValidation:
    """§3.19.3 hard-fail check — the invariant that makes reproducibility work."""

    def _minimal_rubric(self, ids: list[str]) -> dict:
        return {
            "rubric_version": "cross_v1",
            "sections": [
                {"id": i, "history_id": i, "name": i, "section_number": n, "score_type": "numeric"}
                for n, i in enumerate(ids, start=1)
            ],
            "scoring_prompt": {
                "system_prompt_template": "p", "confidence_levels_note": "n",
                "sop_sections": [], "long_call_focus_sections": [], "audio_dependent_sections": [],
            },
        }

    def test_missing_section_in_rubric_raises(self):
        rubric = Rubric.model_validate(self._minimal_rubric(["a"]))
        formula = Formula.model_validate({
            **_minimal_formula(),
            "sections": [
                {"key": "a", "label": "A", "score_type": "rating_1_5", "weight": 100.0},
            ],
        })
        # Now the rubric drops "b" — formula still fine, but if we swap a
        # formula that references "b" the check fails.
        formula2 = Formula.model_validate({
            **_minimal_formula(),
            "sections": [
                {"key": "a", "label": "A", "score_type": "rating_1_5", "weight": 50.0},
                {"key": "b", "label": "B", "score_type": "rating_1_5", "weight": 50.0},
            ],
        })
        validate_formula_against_rubric(formula, rubric)  # OK
        with pytest.raises(FormulaRubricMismatchError) as exc_info:
            validate_formula_against_rubric(formula2, rubric)
        assert exc_info.value.missing == ["b"]

    def test_rule_referenced_section_missing_raises(self):
        rubric = Rubric.model_validate(self._minimal_rubric(["a"]))
        formula = Formula.model_validate({
            **_minimal_formula(),
            "sections": [
                {"key": "a", "label": "A", "score_type": "rating_1_5", "weight": 100.0},
            ],
            "rules": [{
                "id": "wt", "type": "weight_transfer", "enabled": True,
                "when": {"section": "phantom", "equals": "NA"},
                "effect": {"from": "phantom", "to": "a", "amount": "all"},
            }],
            "evaluation_order": ["wt", WEIGHTED_SUM],
        })
        with pytest.raises(FormulaRubricMismatchError):
            validate_formula_against_rubric(formula, rubric)


# ---------------------------------------------------------------------------
# EvaluationSection — §3.5 DB row constraints
# ---------------------------------------------------------------------------

class TestEvaluationSectionValidators:

    def test_numeric_requires_numeric_score(self):
        with pytest.raises(ValidationError, match="requires numeric_score"):
            EvaluationSection.model_validate({
                "section_id": "greeting", "section_number": 1,
                "score_type": "numeric", "score_source": "ai",
                "ai_provider": "gemini",
                "binary_value": "Y",  # wrong shape
            })

    def test_binary_requires_binary_value(self):
        with pytest.raises(ValidationError, match="requires binary_value"):
            EvaluationSection.model_validate({
                "section_id": "cri", "section_number": 10,
                "score_type": "binary", "score_source": "ai",
                "ai_provider": "gemini",
                "numeric_score": 5,  # wrong shape
            })

    def test_ai_source_requires_ai_provider(self):
        with pytest.raises(ValidationError, match="score_source='ai' requires ai_provider"):
            EvaluationSection.model_validate({
                "section_id": "greeting", "section_number": 1,
                "score_type": "numeric", "score_source": "ai",
                "numeric_score": 4,
            })

    def test_non_ai_source_forbids_ai_provider(self):
        with pytest.raises(ValidationError, match="only valid when score_source='ai'"):
            EvaluationSection.model_validate({
                "section_id": "greeting", "section_number": 1,
                "score_type": "manual_numeric", "score_source": "manual",
                "ai_provider": "gemini",
                "numeric_score": 4,
            })

    def test_numeric_score_bounds(self):
        with pytest.raises(ValidationError):
            EvaluationSection.model_validate({
                "section_id": "greeting", "section_number": 1,
                "score_type": "numeric", "score_source": "ai",
                "ai_provider": "gemini",
                "numeric_score": 6,  # > 5
            })

    def test_numeric_na_shape_allowed(self):
        """Migration 012 / §3.8 point 7: unscored na_default sections persist
        as numeric_score NULL + binary_value 'NA'."""
        section = EvaluationSection.model_validate({
            "section_id": "human_review_required", "section_number": 9,
            "score_type": "manual_numeric", "score_source": "manual_default",
            "binary_value": "NA",
        })
        assert section.numeric_score is None
        assert section.binary_value == "NA"

    def test_numeric_with_yn_binary_still_invalid(self):
        with pytest.raises(ValidationError, match="requires numeric_score"):
            EvaluationSection.model_validate({
                "section_id": "human_review_required", "section_number": 9,
                "score_type": "manual_numeric", "score_source": "manual",
                "binary_value": "Y",  # only 'NA' is legal on numeric rows
            })

    def test_numeric_na_alongside_score_invalid(self):
        with pytest.raises(ValidationError, match="requires numeric_score"):
            EvaluationSection.model_validate({
                "section_id": "human_review_required", "section_number": 9,
                "score_type": "manual_numeric", "score_source": "manual",
                "numeric_score": 3, "binary_value": "NA",
            })


# ---------------------------------------------------------------------------
# AssessmentSection, ModelsUsed, AnnotatedTranscript, RecordingUrls
# ---------------------------------------------------------------------------

class TestAssessmentSection:

    def test_valid_trend(self):
        s = AssessmentSection.model_validate({
            "section_id": "greeting", "section_name": "Greeting", "section_number": 1,
            "trend": "improving", "summary": "up", "coaching_tip": "keep going",
        })
        assert s.trend == "improving"

    def test_invalid_trend_raises(self):
        with pytest.raises(ValidationError):
            AssessmentSection.model_validate({
                "section_id": "greeting", "section_name": "Greeting", "section_number": 1,
                "trend": "flat", "summary": "x", "coaching_tip": "y",
            })


class TestModelsUsed:

    def test_gemini_only_cascade(self):
        m = ModelsUsed.model_validate({
            "text": {"provider": "gemini", "model": "gemini-2.5-flash"},
        })
        assert m.text.model == "gemini-2.5-flash"
        assert m.audio is None
        assert m.fallback is None

    def test_landgpt_cascade_with_fallback(self):
        m = ModelsUsed.model_validate({
            "audio": {"provider": "landgpt", "model": "qwen2-audio-v1"},
            "text": {"provider": "landgpt", "model": "gemma-4-27b-q4"},
            "fallback": {"provider": "gemini", "sections": ["matching"], "reason": "audio_dep_low"},
        })
        assert m.fallback.sections == ["matching"]


class TestAnnotatedTranscript:

    def test_loads(self):
        t = AnnotatedTranscript.model_validate({
            "schema_version": "qwen2_audio_v1",
            "language_detected": "en",
            "turns": [
                {"speaker": "agent", "text": "hello", "start_ms": 0, "end_ms": 1000},
                {"speaker": "caller", "text": "hi"},
            ],
        })
        assert len(t.turns) == 2

    def test_bad_speaker_raises(self):
        with pytest.raises(ValidationError):
            AnnotatedTranscript.model_validate({
                "schema_version": "v1",
                "turns": [{"speaker": "narrator", "text": "..."}],
            })


class TestRecordingUrls:

    def test_defaults_to_empty_lists(self):
        r = RecordingUrls.model_validate({})
        assert r.audio == []
        assert r.screen == []

    def test_populated(self):
        r = RecordingUrls.model_validate({
            "audio": ["https://x/a.mp3"],
            "screen": ["https://x/s.mp4"],
        })
        assert r.audio[0].endswith("a.mp3")

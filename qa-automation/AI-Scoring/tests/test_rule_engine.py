"""Rule engine tests — Wave 2 Phase 2a.

Exercises evaluate_formula() against the shipped MS overall_formula.json and
the Sales §8 canonical config. The Sales §11 worked example (expected 75.00)
is the only spec-guaranteed vector — Phase 2c adds sheet-parity golden
fixtures on top.

Hand-computed MS expectations reference member_support_scoring_migration.md:
the §3 "automated" weight column (+10/9 per active section when
human_review_required = NA) and the §6 evaluation order.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.models.formula import WEIGHTED_SUM, Formula
from backend.services.rule_engine import (
    AnswerValidationError,
    FormulaOrderError,
    RuleApplicationError,
    UnhandledNaWeightError,
    evaluate_formula,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_MS_FORMULA_JSON = _REPO_ROOT / "backend" / "config" / "scoring" / "member_support" / "overall_formula.json"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def ms_formula() -> Formula:
    return Formula.model_validate(json.loads(_MS_FORMULA_JSON.read_text(encoding="utf-8")))


def ms_answers(**overrides):
    """Perfect call: all 5s / Y, human_review_required at its NA default."""
    base = {
        "greeting": 5,
        "caller_id": "Y",
        "purpose": 5,
        "matching": 5,
        "process_adherence": 5,
        "call_resolution": 5,
        "comms": 5,
        "efficiency": 5,
        "human_review_required": "NA",
        "cri": "Y",
    }
    base.update(overrides)
    return base


@pytest.fixture(scope="module")
def sales_formula() -> Formula:
    """Sales §8 canonical config (inline until §10 sign-off ships the file)."""
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
            {"key": "greeting",           "label": "Greeting",              "score_type": "binary_yn_na",  "weight": 5.0},
            {"key": "stay_type",          "label": "Personal or COHO Stay", "score_type": "binary_yn_na",  "weight": 4.0},
            {"key": "move_reason",        "label": "Move Reason",           "score_type": "rating_1_5_na", "weight": 15.0},
            {"key": "landing_intro",      "label": "Landing Intro",         "score_type": "binary_yn_na",  "weight": 6.0},
            {"key": "timeline_housing",   "label": "Timeline & Housing",    "score_type": "rating_1_5_na", "weight": 8.0},
            {"key": "landing_guarantee",  "label": "Landing Guarantee",     "score_type": "rating_1_5_na", "weight": 6.0},
            {"key": "pricing",            "label": "Pricing Breakdown",     "score_type": "rating_1_5_na", "weight": 8.0},
            {"key": "fit_confirmation",   "label": "Confirmation of Fit",   "score_type": "rating_1_5_na", "weight": 5.0},
            {"key": "objection_handling", "label": "Objection Handling",    "score_type": "rating_1_5_na", "weight": 8.0},
            {"key": "urgency",            "label": "Urgency",               "score_type": "binary_yn_na",  "weight": 5.0},
            {"key": "follow_up",          "label": "Follow-up",             "score_type": "binary_yn_na",  "weight": 6.0},
            {"key": "potential_booking",  "label": "Potential Booking",     "score_type": "binary_yn_na",  "weight": 4.0},
            {"key": "notes_mc",           "label": "Detailed Notes in MC",  "score_type": "binary_yn_na",  "weight": 5.0},
            {"key": "contact_shared",     "label": "Contact Shared",        "score_type": "binary_yn_na",  "weight": 5.0},
            {"key": "client_experience",  "label": "Client Experience",     "score_type": "rating_1_5_na", "weight": 10.0},
        ],
        "rules": [],
        "evaluation_order": [WEIGHTED_SUM],
        "triggers": {},
        "external_signals": {},
        "deprecated_sections": [],
    })


def _formula(**overrides) -> Formula:
    """Minimal two-section formula, mutated per test."""
    base = {
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
    base.update(overrides)
    return Formula.model_validate(base)


# ---------------------------------------------------------------------------
# Sales — full_credit NA, no rules (spec §4/§6/§11)
# ---------------------------------------------------------------------------

class TestSalesFullCredit:

    # Spec §11 answers, with the PDF's 0/1 binary labels mapped to N/Y.
    SALES_S11_ANSWERS = {
        "greeting": "Y",
        "stay_type": "N",
        "move_reason": 4,
        "landing_intro": "Y",
        "timeline_housing": "NA",
        "landing_guarantee": 3,
        "pricing": 5,
        "fit_confirmation": 2,
        "objection_handling": 4,
        "urgency": "Y",
        "follow_up": "N",
        "potential_booking": "Y",
        "notes_mc": "Y",
        "contact_shared": "NA",
        "client_experience": 4,
    }

    def test_spec_s11_worked_example_is_75(self, sales_formula):
        result = evaluate_formula(sales_formula, self.SALES_S11_ANSWERS)
        assert result.final_score == pytest.approx(75.00, abs=1e-9)
        assert result.raw_score == pytest.approx(75.00, abs=1e-9)

    def test_na_sections_contribute_full_weight(self, sales_formula):
        result = evaluate_formula(sales_formula, self.SALES_S11_ANSWERS)
        assert result.contributions["timeline_housing"] == pytest.approx(8.0)
        assert result.contributions["contact_shared"] == pytest.approx(5.0)
        assert result.fracs["timeline_housing"] == 1.0

    def test_weights_are_static_under_full_credit(self, sales_formula):
        result = evaluate_formula(sales_formula, self.SALES_S11_ANSWERS)
        assert result.effective_weights == {s.key: s.weight for s in sales_formula.sections}

    def test_all_na_scores_100(self, sales_formula):
        answers = {s.key: "NA" for s in sales_formula.sections}
        result = evaluate_formula(sales_formula, answers)
        assert result.final_score == pytest.approx(100.0)

    def test_no_events_no_trace(self, sales_formula):
        result = evaluate_formula(sales_formula, self.SALES_S11_ANSWERS)
        assert result.events == []
        assert result.trace == []


# ---------------------------------------------------------------------------
# Member Support — redistribute_per_rules (spec §3-§6)
# ---------------------------------------------------------------------------

class TestMemberSupportEngine:

    def test_perfect_call_hrr_na_scores_100(self, ms_formula):
        result = evaluate_formula(ms_formula, ms_answers())
        assert result.final_score == pytest.approx(100.0)
        assert result.na_sections == ["human_review_required"]

    def test_hrr_na_spread_matches_spec_automated_weights(self, ms_formula):
        """§3 automated column: 9 active sections gain +10/9 ≈ 1.11 each."""
        result = evaluate_formula(ms_formula, ms_answers())
        w = result.effective_weights
        assert w["human_review_required"] == pytest.approx(0.0)
        assert w["greeting"] == pytest.approx(5 + 10 / 9)     # 6.11%
        assert w["process_adherence"] == pytest.approx(25 + 10 / 9)  # 26.11%
        assert w["call_resolution"] == pytest.approx(20 + 10 / 9)    # 21.11%
        assert sum(w.values()) == pytest.approx(100.0)

    def test_all_fours_standard_automated_case(self, ms_formula):
        answers = ms_answers(
            greeting=4, purpose=4, matching=4, process_adherence=4,
            call_resolution=4, comms=4, efficiency=4,
        )
        result = evaluate_formula(ms_formula, answers)
        # ratings 0.75 × (80 + 7·10/9) + binary 1.0 × (10 + 2·10/9)
        assert result.final_score == pytest.approx(0.75 * (80 + 70 / 9) + (10 + 20 / 9))

    def test_caller_id_na_transfers_weight_to_call_resolution(self, ms_formula):
        result = evaluate_formula(ms_formula, ms_answers(caller_id="NA"))
        w = result.effective_weights
        assert w["caller_id"] == pytest.approx(0.0)
        # 20 + 5 transferred + 10/8 from the hrr spread over 8 active sections
        assert w["call_resolution"] == pytest.approx(25 + 10 / 8)
        assert result.final_score == pytest.approx(100.0)
        assert result.na_sections == ["caller_id", "human_review_required"]

    def test_caller_id_n_scales_final_by_half(self, ms_formula):
        result = evaluate_formula(ms_formula, ms_answers(caller_id="N"))
        # caller_id stays active (frac 0, weight 5 + 10/9); raw loses only its slot
        assert result.raw_score == pytest.approx(100 - (5 + 10 / 9))
        assert result.final_score == pytest.approx((100 - (5 + 10 / 9)) * 0.5)
        assert not result.overridden

    def test_hard_zero_disabled_never_fires(self, ms_formula):
        result = evaluate_formula(ms_formula, ms_answers(caller_id="N"))
        hard_zero = next(t for t in result.trace if t.rule_id == "hard_zero")
        assert not hard_zero.fired
        assert hard_zero.note == "disabled"

    def test_frequent_caller_shift_moves_half_of_current_resolution_weight(self, ms_formula):
        result = evaluate_formula(
            ms_formula, ms_answers(), signals={"frequent_caller": True}
        )
        w = result.effective_weights
        # 0.5 × 20 moves resolution → process, then the hrr spread adds 10/9 each
        assert w["process_adherence"] == pytest.approx(35 + 10 / 9)
        assert w["call_resolution"] == pytest.approx(10 + 10 / 9)

    def test_transfer_then_shift_compound_in_evaluation_order(self, ms_formula):
        """caller_id_na_transfer runs first (§6), so the shift moves half of
        the *boosted* call_resolution weight (25, not 20)."""
        result = evaluate_formula(
            ms_formula, ms_answers(caller_id="NA"), signals={"frequent_caller": True}
        )
        w = result.effective_weights
        assert w["process_adherence"] == pytest.approx(25 + 12.5 + 10 / 8)
        assert w["call_resolution"] == pytest.approx(12.5 + 10 / 8)

    def test_signal_absent_means_false(self, ms_formula):
        result = evaluate_formula(ms_formula, ms_answers())
        shift = next(t for t in result.trace if t.rule_id == "frequent_caller_shift")
        assert not shift.fired

    def test_escalation_flag_fires_at_rating_2_and_below(self, ms_formula):
        """Threshold tightened 1-3 -> 1-2 per Ops VP sign-off 2026-07-04
        (member_support_v3): rating 3 no longer escalates."""
        for rating, should_fire in [(1, True), (2, True), (3, False), (4, False), (5, False)]:
            result = evaluate_formula(ms_formula, ms_answers(process_adherence=rating))
            fired = any(e.rule_id == "escalation_flag" for e in result.events)
            assert fired is should_fire, f"rating {rating}"

    def test_escalation_event_shape_and_score_neutrality(self, ms_formula):
        result = evaluate_formula(ms_formula, ms_answers(call_resolution=1))
        event = next(e for e in result.events if e.rule_id == "escalation_flag")
        assert event.event == "human_review_required"
        assert event.route == "supervisor_review"
        # flag never touches the score: only resolution's own slot is lost
        assert result.final_score == pytest.approx(100 - (20 + 10 / 9))

    def test_hrr_scored_by_supervisor_keeps_base_weights(self, ms_formula):
        """When human review actually happened (HRR = 1), nothing is NA and
        base weights apply unchanged."""
        result = evaluate_formula(ms_formula, ms_answers(human_review_required=1))
        assert result.effective_weights == {s.key: s.weight for s in ms_formula.sections}
        # HRR frac 0 → loses its 10 points
        assert result.final_score == pytest.approx(90.0)
        assert result.na_sections == []


class TestScoreOverride:

    def _hard_zero_enabled(self, ms_formula) -> Formula:
        raw = ms_formula.model_dump(by_alias=True)
        rule = next(r for r in raw["rules"] if r["id"] == "hard_zero")
        rule["enabled"] = True
        return Formula.model_validate(raw)

    def test_hard_zero_enabled_overrides_final_score(self, ms_formula):
        formula = self._hard_zero_enabled(ms_formula)
        result = evaluate_formula(formula, ms_answers(caller_id="N"))
        assert result.final_score == 0.0
        assert result.overridden
        # raw score stays observable for the trace/parity checks
        assert result.raw_score == pytest.approx(100 - (5 + 10 / 9))

    def test_override_skips_later_score_scale(self, ms_formula):
        formula = self._hard_zero_enabled(ms_formula)
        result = evaluate_formula(formula, ms_answers(caller_id="N"))
        scale = next(t for t in result.trace if t.rule_id == "caller_id_scale_half")
        assert scale.fired
        assert "skipped" in (scale.note or "")

    def test_override_does_not_suppress_flags(self, ms_formula):
        formula = self._hard_zero_enabled(ms_formula)
        result = evaluate_formula(
            formula, ms_answers(caller_id="N", process_adherence=1)
        )
        assert result.overridden
        assert any(e.rule_id == "escalation_flag" for e in result.events)


# ---------------------------------------------------------------------------
# NA safety net — MS §4 "never silently lost"
# ---------------------------------------------------------------------------

class TestUnhandledNaWeight:

    def test_na_with_no_rule_raises_under_redistribute(self):
        formula = _formula(
            normalization={
                "rating_1_5": {"type": "linear", "input_min": 1, "input_max": 5, "output": [0.0, 1.0]},
                "binary_yn": {"Y": 1.0, "N": 0.0},
                "na": "redistribute_per_rules",
            },
            sections=[
                {"key": "a", "label": "A", "score_type": "rating_1_5_na", "weight": 60.0},
                {"key": "b", "label": "B", "score_type": "rating_1_5", "weight": 40.0},
            ],
        )
        with pytest.raises(UnhandledNaWeightError) as exc:
            evaluate_formula(formula, {"a": "NA", "b": 5})
        assert exc.value.stranded == {"a": 60.0}

    def test_same_shape_is_fine_under_full_credit(self):
        formula = _formula(
            sections=[
                {"key": "a", "label": "A", "score_type": "rating_1_5_na", "weight": 60.0},
                {"key": "b", "label": "B", "score_type": "rating_1_5", "weight": 40.0},
            ],
        )
        result = evaluate_formula(formula, {"a": "NA", "b": 5})
        assert result.final_score == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# Generic rule mechanics (beyond the two shipped formulas)
# ---------------------------------------------------------------------------

class TestRuleMechanics:

    def test_na_redistribution_rule_is_proportional(self):
        """SQLMigration §3.8: 'a' NA → its 20 spreads 60:20 across b/c."""
        formula = _formula(
            normalization={
                "rating_1_5": {"type": "linear", "input_min": 1, "input_max": 5, "output": [0.0, 1.0]},
                "binary_yn": {"Y": 1.0, "N": 0.0},
                "na": "redistribute_per_rules",
            },
            sections=[
                {"key": "a", "label": "A", "score_type": "rating_1_5_na", "weight": 20.0},
                {"key": "b", "label": "B", "score_type": "rating_1_5", "weight": 60.0},
                {"key": "c", "label": "C", "score_type": "rating_1_5", "weight": 20.0},
            ],
            rules=[{
                "id": "a_na", "type": "na_redistribution", "enabled": True,
                "when": {"section": "a", "equals": "NA"},
                "mode": "proportional", "targets": "remaining",
            }],
            evaluation_order=["a_na", WEIGHTED_SUM],
        )
        result = evaluate_formula(formula, {"a": "NA", "b": 5, "c": 1})
        assert result.effective_weights["b"] == pytest.approx(75.0)  # 60 + 20·(60/80)
        assert result.effective_weights["c"] == pytest.approx(25.0)  # 20 + 20·(20/80)
        assert result.final_score == pytest.approx(75.0)

    def test_weight_transfer_split_targets(self):
        formula = _formula(
            sections=[
                {"key": "a", "label": "A", "score_type": "rating_1_5", "weight": 20.0},
                {"key": "b", "label": "B", "score_type": "rating_1_5", "weight": 40.0},
                {"key": "c", "label": "C", "score_type": "rating_1_5", "weight": 40.0},
            ],
            rules=[{
                "id": "split", "type": "weight_transfer", "enabled": True,
                "when": {"section": "a", "frac_lte": 1.0},
                "effect": {"from": "a", "to": {"b": 0.75, "c": 0.25}, "amount": "all"},
            }],
            evaluation_order=["split", WEIGHTED_SUM],
        )
        result = evaluate_formula(formula, {"a": 5, "b": 5, "c": 5})
        assert result.effective_weights == pytest.approx({"a": 0.0, "b": 55.0, "c": 45.0})

    def test_weight_transfer_bad_shares_raise(self):
        formula = _formula(
            sections=[
                {"key": "a", "label": "A", "score_type": "rating_1_5", "weight": 20.0},
                {"key": "b", "label": "B", "score_type": "rating_1_5", "weight": 40.0},
                {"key": "c", "label": "C", "score_type": "rating_1_5", "weight": 40.0},
            ],
            rules=[{
                "id": "split", "type": "weight_transfer", "enabled": True,
                "when": {"section": "a", "frac_lte": 1.0},
                "effect": {"from": "a", "to": {"b": 0.5, "c": 0.25}, "amount": "all"},
            }],
            evaluation_order=["split", WEIGHTED_SUM],
        )
        with pytest.raises(RuleApplicationError, match="shares sum"):
            evaluate_formula(formula, {"a": 5, "b": 5, "c": 5})

    def test_frac_gte_condition(self):
        formula = _formula(
            rules=[{
                "id": "high_a", "type": "flag", "enabled": True,
                "when": {"section": "a", "frac_gte": 0.75},
                "effect": {"emit_event": "kudos"},
            }],
            evaluation_order=["high_a", WEIGHTED_SUM],
        )
        fired = evaluate_formula(formula, {"a": 4, "b": 3})
        assert any(e.event == "kudos" for e in fired.events)
        not_fired = evaluate_formula(formula, {"a": 3, "b": 3})
        assert not_fired.events == []

    def test_equals_matches_rating_answers_as_strings(self):
        formula = _formula(
            rules=[{
                "id": "exact_three", "type": "flag", "enabled": True,
                "when": {"section": "a", "equals": "3"},
                "effect": {"emit_event": "mid"},
            }],
            evaluation_order=["exact_three", WEIGHTED_SUM],
        )
        assert evaluate_formula(formula, {"a": 3, "b": 5}).events != []
        assert evaluate_formula(formula, {"a": 4, "b": 5}).events == []

    def test_when_clause_without_condition_raises(self):
        formula = _formula(
            rules=[{
                "id": "vacuous", "type": "flag", "enabled": True,
                "when": {"section": "a"},
                "effect": {"emit_event": "noop"},
            }],
            evaluation_order=["vacuous", WEIGHTED_SUM],
        )
        with pytest.raises(RuleApplicationError, match="no condition"):
            evaluate_formula(formula, {"a": 5, "b": 5})

    def test_stacked_score_scales_multiply(self):
        formula = _formula(
            rules=[
                {"id": "s1", "type": "score_scale", "enabled": True,
                 "when": {"section": "a", "frac_lte": 1.0}, "effect": {"multiply": 0.5}},
                {"id": "s2", "type": "score_scale", "enabled": True,
                 "when": {"section": "a", "frac_lte": 1.0}, "effect": {"multiply": 0.9}},
            ],
            evaluation_order=[WEIGHTED_SUM, "s1", "s2"],
        )
        result = evaluate_formula(formula, {"a": 5, "b": 5})
        assert result.final_score == pytest.approx(45.0)
        assert result.raw_score == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# Static evaluation_order misconfigurations
# ---------------------------------------------------------------------------

class TestStaticOrderChecks:

    def test_weight_rule_after_weighted_sum_raises(self):
        formula = _formula(
            rules=[{
                "id": "late_move", "type": "weight_transfer", "enabled": True,
                "when": {"section": "a", "frac_lte": 1.0},
                "effect": {"from": "a", "to": "b", "amount": "all"},
            }],
            evaluation_order=[WEIGHTED_SUM, "late_move"],
        )
        with pytest.raises(FormulaOrderError, match="after"):
            evaluate_formula(formula, {"a": 5, "b": 5})

    def test_score_scale_before_weighted_sum_raises(self):
        formula = _formula(
            rules=[{
                "id": "early_scale", "type": "score_scale", "enabled": True,
                "when": {"section": "a", "frac_lte": 1.0}, "effect": {"multiply": 0.5},
            }],
            evaluation_order=["early_scale", WEIGHTED_SUM],
        )
        with pytest.raises(FormulaOrderError, match="before"):
            evaluate_formula(formula, {"a": 5, "b": 5})

    def test_duplicate_weighted_sum_raises(self):
        formula = _formula(evaluation_order=[WEIGHTED_SUM, WEIGHTED_SUM])
        with pytest.raises(FormulaOrderError, match="exactly one"):
            evaluate_formula(formula, {"a": 5, "b": 5})

    def test_static_checks_run_before_answer_validation(self):
        """Misconfigured order fails even with garbage answers — the check is
        data-independent."""
        formula = _formula(evaluation_order=[WEIGHTED_SUM, WEIGHTED_SUM])
        with pytest.raises(FormulaOrderError):
            evaluate_formula(formula, {})


# ---------------------------------------------------------------------------
# Strict answer validation (Wave2Plan §9 remapping risk)
# ---------------------------------------------------------------------------

class TestAnswerValidation:

    def test_missing_answer_raises(self, ms_formula):
        answers = ms_answers()
        del answers["cri"]
        with pytest.raises(AnswerValidationError, match="missing answers.*cri"):
            evaluate_formula(ms_formula, answers)

    def test_unknown_section_key_raises(self, ms_formula):
        """Legacy history_id keys must not silently score — §9 remapping."""
        answers = ms_answers()
        answers["identity_validation"] = answers.pop("caller_id")
        with pytest.raises(AnswerValidationError, match="unknown sections"):
            evaluate_formula(ms_formula, answers)

    def test_na_on_non_na_section_raises(self, ms_formula):
        with pytest.raises(AnswerValidationError, match="NA not allowed"):
            evaluate_formula(ms_formula, ms_answers(greeting="NA"))

    @pytest.mark.parametrize("bad", [0, 6, "5", 4.5, True, None])
    def test_bad_rating_values_raise(self, ms_formula, bad):
        with pytest.raises(AnswerValidationError):
            evaluate_formula(ms_formula, ms_answers(greeting=bad))

    @pytest.mark.parametrize("bad", ["y", "yes", 1, "1", None])
    def test_bad_binary_values_raise(self, ms_formula, bad):
        """Sales' 0/1 labels are NOT accepted — callers map to Y/N first
        (int 1 would be ambiguous with rating 1)."""
        with pytest.raises(AnswerValidationError):
            evaluate_formula(ms_formula, ms_answers(cri=bad))

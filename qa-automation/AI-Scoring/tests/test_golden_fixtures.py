"""Golden fixture tests — Wave 2 Phase 2c (BackfillPlan.md §6).

Real Analyst_History rows (anonymized) with their sheet-computed overall
scores, asserted against the engine under the archived legacy formula
member_support_v0_sheet. This is exact-parity regression armor: if the
engine's normalization, NA redistribution, or rounding drifts, these fail.

Fixture provenance: scripts/extract_golden_fixtures.py over the (gitignored)
seed CSV — 1,668/1,676 scoreable rows matched exactly at extraction time;
the sampled 40 + the 3 hand-edited anomaly rows live here.
"""

from __future__ import annotations

import json
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pytest

from backend.models.formula import (
    WEIGHTED_SUM,
    Formula,
    Rubric,
    validate_formula_against_rubric,
)
from backend.services.rule_engine import evaluate_formula

_AI_SCORING = Path(__file__).resolve().parent.parent
_V0_FORMULA = _AI_SCORING / "backend" / "config" / "scoring" / "member_support" / "v0_sheet.json"
_FIXTURES = _AI_SCORING / "tests" / "fixtures" / "overall_formula" / "member_support.json"


@pytest.fixture(scope="module")
def v0_formula() -> Formula:
    return Formula.model_validate(json.loads(_V0_FORMULA.read_text(encoding="utf-8")))


@pytest.fixture(scope="module")
def fixture_doc() -> dict:
    return json.loads(_FIXTURES.read_text(encoding="utf-8"))


def _sheet_round(score: float) -> int:
    """The sheet displays integer scores; half-up matches its ROUND()."""
    return int(Decimal(str(score)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


class TestV0SheetFormula:
    """The archived legacy formula itself (BackfillPlan.md §2)."""

    def test_validates_and_weights_sum_to_100(self, v0_formula):
        assert v0_formula.formula_id == "member_support_v0_sheet"
        assert sum(s.weight for s in v0_formula.sections) == pytest.approx(100.0)

    def test_four_sections_carry_zero_weight(self, v0_formula):
        zeros = {s.key for s in v0_formula.sections if s.weight == 0.0}
        assert zeros == {
            "greeting", "caller_identity_validation",
            "matching_the_moment", "process_adherence",
        }

    def test_cross_validates_against_the_archived_v1_rubric(self, v0_formula):
        """v0_sheet pins rubric_version=member_support_v1 — the migration-010
        seed already live in qa.rubric_versions. The §3.19.3 check must hold
        for that exact content or compute_overall_score() rejects every
        backfilled row. Parses the rubric out of the migration SQL so drift
        between repo formula and shipped seed fails here, not in production."""
        sql = (_AI_SCORING.parent.parent / "database" / "migrations"
               / "010_rubric_versioning.sql").read_text(encoding="utf-8")
        marker = "$rubric_ms_v1$"
        start = sql.index(marker) + len(marker)
        rubric_json = sql[start: sql.index(marker, start)].strip()
        rubric = Rubric.model_validate(json.loads(rubric_json))
        assert rubric.rubric_version == v0_formula.rubric_version
        validate_formula_against_rubric(v0_formula, rubric)  # raises on drift

    def test_twelfth_weights(self, v0_formula):
        w = {s.key: s.weight for s in v0_formula.sections}
        assert w["call_resolution"] == pytest.approx(400 / 12)
        assert w["documentation"] == pytest.approx(200 / 12)
        assert w["purpose_of_call"] == pytest.approx(100 / 12)

    def test_r_over_5_curve(self, v0_formula):
        """output [0.2, 1.0] == the sheet's rating/5 (rating 1 → 20%, not 0%)."""
        assert v0_formula.normalization.rating_1_5.output == [0.2, 1.0]

    def test_na_redistributes_per_rules(self, v0_formula):
        assert v0_formula.normalization.na == "redistribute_per_rules"
        assert [t for t in v0_formula.evaluation_order if t != WEIGHTED_SUM] == [
            "civ_na_redistribute", "cri_na_redistribute",
        ]


class TestGoldenFixtures:

    def test_fixture_inventory(self, fixture_doc):
        fixtures = fixture_doc["fixtures"]
        exact = [f for f in fixtures if f["expect_exact"]]
        anomalies = [f for f in fixtures if not f["expect_exact"]]
        assert len(exact) == 40
        assert len(anomalies) == 3
        assert {f["era"] for f in exact} == {"ai", "manual"}
        assert any("NA" in f["answers"].values() for f in exact), \
            "sample must exercise the NA-redistribute path"

    def test_exact_parity_with_sheet(self, v0_formula, fixture_doc):
        failures = []
        for f in fixture_doc["fixtures"]:
            if not f["expect_exact"]:
                continue
            result = evaluate_formula(v0_formula, f["answers"])
            got = _sheet_round(result.final_score)
            if got != f["sheet_overall"]:
                failures.append(f"{f['label']}: engine {got} != sheet {f['sheet_overall']}")
        assert not failures, "\n".join(failures)

    def test_hand_edited_rows_stay_anomalous(self, v0_formula, fixture_doc):
        """The 3 hand-edits document real sheet edits — if the engine ever
        'matches' one, the formula was changed to chase an anomaly."""
        for f in fixture_doc["fixtures"]:
            if f["expect_exact"]:
                continue
            result = evaluate_formula(v0_formula, f["answers"])
            assert _sheet_round(result.final_score) != f["sheet_overall"], f["label"]
            assert _sheet_round(result.final_score) == f["engine_overall"], f["label"]

    def test_na_fixtures_redistribute_not_full_credit(self, v0_formula, fixture_doc):
        """On NA rows the CRI weight must spread over active sections —
        full-credit would inflate the score (BackfillPlan.md §2 evidence)."""
        na_fixtures = [
            f for f in fixture_doc["fixtures"]
            if f["expect_exact"] and f["answers"]["customer_resolution_indicator"] == "NA"
        ]
        assert na_fixtures, "sample must include a CRI=NA fixture"
        for f in na_fixtures:
            result = evaluate_formula(v0_formula, f["answers"])
            assert result.effective_weights["customer_resolution_indicator"] == 0.0
            assert sum(result.effective_weights.values()) == pytest.approx(100.0)
            assert _sheet_round(result.final_score) == f["sheet_overall"]

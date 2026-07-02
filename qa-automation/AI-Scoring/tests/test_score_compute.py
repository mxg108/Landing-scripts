"""compute_overall_score tests — Wave 2 Phase 2b.

Runs the full §3.6 pipeline against a stub asyncpg connection seeded with
the shipped MS formula/rubric as archive rows. No live DB — the queries are
exercised through the same fetchrow()/fetch() surface asyncpg exposes.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from backend.models.formula import FormulaRubricMismatchError
from backend.services.score_compute import (
    EvaluationNotFoundError,
    MissingVersionStampError,
    ScoreComputeError,
    VersionNotArchivedError,
    build_answers,
    compute_overall_score,
    compute_overall_score_with_overrides,
    compute_score_detail,
    get_active_versions,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_MS_FORMULA_JSON = _REPO_ROOT / "backend" / "config" / "scoring" / "member_support" / "overall_formula.json"
_MS_TEAM_JSON = _REPO_ROOT / "backend" / "config" / "teams" / "member_support.json"

MS_FORMULA_VERSION = "member_support_v2"
MS_RUBRIC_VERSION = "member_support_v2"


# ---------------------------------------------------------------------------
# Stub connection — asyncpg's fetchrow()/fetch() surface over dict fixtures
# ---------------------------------------------------------------------------

class FakeConn:
    def __init__(self, evaluations=None, formulas=None, rubrics=None, sections=None):
        self.evaluations = evaluations or {}
        self.formulas = formulas or {}      # version -> (team_id, formula_json, effective_until)
        self.rubrics = rubrics or {}        # version -> (team_id, rubric_json, effective_until)
        self.sections = sections or {}      # evaluation_id -> [row dicts]

    async def fetchrow(self, query, *args):
        if "FROM qa.evaluations" in query:
            return self.evaluations.get(args[0])
        if "formula_json FROM qa.formula_versions" in query:
            entry = self.formulas.get(args[0])
            return {"formula_json": entry[1]} if entry else None
        if "rubric_json FROM qa.rubric_versions" in query:
            entry = self.rubrics.get(args[0])
            return {"rubric_json": entry[1]} if entry else None
        if "formula_version FROM qa.formula_versions" in query:
            for version, (team_id, _, until) in self.formulas.items():
                if team_id == args[0] and until is None:
                    return {"formula_version": version}
            return None
        if "rubric_version FROM qa.rubric_versions" in query:
            for version, (team_id, _, until) in self.rubrics.items():
                if team_id == args[0] and until is None:
                    return {"rubric_version": version}
            return None
        raise AssertionError(f"unexpected fetchrow: {query}")

    async def fetch(self, query, *args):
        assert "FROM qa.evaluation_sections" in query
        return self.sections.get(args[0], [])


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

def _ms_formula_json() -> dict:
    return json.loads(_MS_FORMULA_JSON.read_text(encoding="utf-8"))


def _ms_rubric_json() -> dict:
    return json.loads(_MS_TEAM_JSON.read_text(encoding="utf-8"))["rubric"]


def _eval_row(eval_id=1, formula_version=MS_FORMULA_VERSION, rubric_version=MS_RUBRIC_VERSION):
    return {
        "id": eval_id,
        "team_id": "member_support",
        "formula_version": formula_version,
        "rubric_version": rubric_version,
    }


def _section_rows(answers: dict) -> list[dict]:
    """answers dict → qa.evaluation_sections row shapes. Ints land in
    numeric_score; Y/N/NA strings land in binary_value (the migration-012
    NA-numeric shape falls out naturally: numeric_score NULL + 'NA')."""
    return [
        {
            "section_id": key,
            "numeric_score": value if isinstance(value, int) else None,
            "binary_value": value if isinstance(value, str) else None,
        }
        for key, value in answers.items()
    ]


def _perfect_answers(**overrides):
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


def _conn(answers=None, eval_row=None, rubric_json=None, jsonb_as_str=False):
    formula_json = _ms_formula_json()
    rubric = rubric_json if rubric_json is not None else _ms_rubric_json()
    if jsonb_as_str:
        formula_json = json.dumps(formula_json)
        rubric = json.dumps(rubric)
    return FakeConn(
        evaluations={1: eval_row if eval_row is not None else _eval_row()},
        formulas={MS_FORMULA_VERSION: ("member_support", formula_json, None)},
        rubrics={MS_RUBRIC_VERSION: ("member_support", rubric, None)},
        sections={1: _section_rows(answers if answers is not None else _perfect_answers())},
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

class TestComputeOverallScore:
    pytestmark = pytest.mark.asyncio

    async def test_perfect_eval_scores_100(self):
        score = await compute_overall_score(_conn(), 1)
        assert score == Decimal("100.0")

    async def test_all_fours_rounds_half_up_to_numeric_5_1(self):
        """Engine value is 78.0555… (0.75·(80+70/9) + 10+20/9) → 78.1."""
        answers = _perfect_answers(
            greeting=4, purpose=4, matching=4, process_adherence=4,
            call_resolution=4, comms=4, efficiency=4,
        )
        score = await compute_overall_score(_conn(answers), 1)
        assert score == Decimal("78.1")

    async def test_caller_id_n_halves_score(self):
        """(100 − (5 + 10/9)) × 0.5 = 46.944… → 46.9."""
        score = await compute_overall_score(_conn(_perfect_answers(caller_id="N")), 1)
        assert score == Decimal("46.9")

    async def test_hrr_na_numeric_row_shape_converts(self):
        """The migration-012 shape (numeric section, numeric_score NULL,
        binary_value 'NA') must reach the engine as 'NA'."""
        rows = _section_rows(_perfect_answers())
        hrr = next(r for r in rows if r["section_id"] == "human_review_required")
        assert hrr["numeric_score"] is None and hrr["binary_value"] == "NA"
        detail = await compute_score_detail(_conn(), 1)
        assert detail.result.na_sections == ["human_review_required"]

    async def test_signals_reach_the_engine(self):
        """frequent_caller moves resolution weight before its frac-0 loss:
        without signal 100 − (20+10/9) → 78.9; with it 100 − (10+10/9) → 88.9."""
        answers = _perfect_answers(call_resolution=1)
        without = await compute_overall_score(_conn(answers), 1)
        with_signal = await compute_overall_score(
            _conn(answers), 1, signals={"frequent_caller": True}
        )
        assert without == Decimal("78.9")
        assert with_signal == Decimal("88.9")

    async def test_detail_carries_versions_and_trace(self):
        detail = await compute_score_detail(_conn(_perfect_answers(process_adherence=1)), 1)
        assert detail.team_id == "member_support"
        assert detail.formula_version == MS_FORMULA_VERSION
        assert detail.rubric_version == MS_RUBRIC_VERSION
        assert any(e.rule_id == "escalation_flag" for e in detail.result.events)
        assert detail.overall_score == Decimal(str(detail.result.final_score)).quantize(Decimal("0.1"))

    async def test_jsonb_returned_as_str_is_decoded(self):
        """asyncpg without a JSONB codec returns str — both archives decode."""
        score = await compute_overall_score(_conn(jsonb_as_str=True), 1)
        assert score == Decimal("100.0")


# ---------------------------------------------------------------------------
# Version pinning + overrides
# ---------------------------------------------------------------------------

class TestVersionResolution:
    pytestmark = pytest.mark.asyncio

    async def test_missing_eval_raises(self):
        with pytest.raises(EvaluationNotFoundError):
            await compute_overall_score(_conn(), 999)

    async def test_unstamped_row_raises(self):
        conn = _conn(eval_row=_eval_row(formula_version=None))
        with pytest.raises(MissingVersionStampError, match="formula_version"):
            await compute_overall_score(conn, 1)

    async def test_unstamped_rubric_raises(self):
        conn = _conn(eval_row=_eval_row(rubric_version=None))
        with pytest.raises(MissingVersionStampError, match="rubric_version"):
            await compute_overall_score(conn, 1)

    async def test_overrides_score_unstamped_rows(self):
        """§3.6: pre-cutover/backfilled rows have NULL versions — the sweep
        escape hatch supplies them explicitly."""
        conn = _conn(eval_row=_eval_row(formula_version=None, rubric_version=None))
        score = await compute_overall_score_with_overrides(
            conn, 1,
            formula_version=MS_FORMULA_VERSION,
            rubric_version=MS_RUBRIC_VERSION,
        )
        assert score == Decimal("100.0")

    async def test_override_beats_row_stamp(self):
        conn = _conn(eval_row=_eval_row(formula_version="member_support_v9"))
        score = await compute_overall_score_with_overrides(
            conn, 1, formula_version=MS_FORMULA_VERSION
        )
        assert score == Decimal("100.0")

    async def test_unarchived_version_raises(self):
        conn = _conn(eval_row=_eval_row(formula_version="member_support_v9"))
        with pytest.raises(VersionNotArchivedError, match="member_support_v9"):
            await compute_overall_score(conn, 1)

    async def test_rubric_missing_formula_section_hard_fails(self):
        """§3.19.3 at eval time: archived rubric must cover every section
        the formula references."""
        rubric = _ms_rubric_json()
        rubric["sections"] = [s for s in rubric["sections"] if s["id"] != "cri"]
        with pytest.raises(FormulaRubricMismatchError, match="cri"):
            await compute_overall_score(_conn(rubric_json=rubric), 1)


# ---------------------------------------------------------------------------
# get_active_versions
# ---------------------------------------------------------------------------

class TestActiveVersions:
    pytestmark = pytest.mark.asyncio

    async def test_returns_live_pair(self):
        versions = await get_active_versions(_conn(), "member_support")
        assert versions.formula_version == MS_FORMULA_VERSION
        assert versions.rubric_version == MS_RUBRIC_VERSION

    async def test_no_active_formula_raises(self):
        conn = _conn()
        conn.formulas = {MS_FORMULA_VERSION: ("member_support", _ms_formula_json(), "2026-06-30")}
        with pytest.raises(VersionNotArchivedError, match="active for member_support"):
            await get_active_versions(conn, "member_support")


# ---------------------------------------------------------------------------
# Row → answer conversion (pure)
# ---------------------------------------------------------------------------

class TestBuildAnswers:

    def test_binary_value_wins_and_numeric_converts(self):
        answers = build_answers([
            {"section_id": "caller_id", "numeric_score": None, "binary_value": "NA"},
            {"section_id": "greeting", "numeric_score": 4, "binary_value": None},
        ])
        assert answers == {"caller_id": "NA", "greeting": 4}
        assert isinstance(answers["greeting"], int)

    def test_both_null_raises(self):
        with pytest.raises(ScoreComputeError, match="neither"):
            build_answers([
                {"section_id": "greeting", "numeric_score": None, "binary_value": None},
            ])

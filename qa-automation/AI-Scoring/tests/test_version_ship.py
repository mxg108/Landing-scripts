"""Version-ship tests — Wave 2 Phase 3a.

Exercises the §3.12 formula startup ship and the §3.19.2 rubric programmatic
ship against a stub asyncpg connection, including the immutability guard and
the §3.19.3 cross-checks in both directions.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from backend.config.team_config import get_team_config
from backend.models.formula import Formula, FormulaRubricMismatchError, Rubric
from backend.services.version_ship import (
    ImmutableVersionError,
    RubricNotArchivedError,
    load_active_formula_files,
    run_startup_ship,
    ship_formula,
    ship_rubric,
)

_AI_SCORING = Path(__file__).resolve().parent.parent
_MS_FORMULA = _AI_SCORING / "backend" / "config" / "scoring" / "member_support" / "overall_formula.json"


class FakeConn:
    """fetchrow/execute/transaction surface over in-memory version tables."""

    def __init__(self, formulas=None, rubrics=None):
        # version -> {"team_id","json","effective_until"}
        self.formulas = formulas or {}
        self.rubrics = rubrics or {}
        self.executed: list[str] = []

    @asynccontextmanager
    async def transaction(self):
        yield

    async def fetchrow(self, query, *args):
        if "formula_json FROM qa.formula_versions WHERE formula_version" in query:
            e = self.formulas.get(args[0])
            return {"formula_json": e["json"]} if e else None
        if "rubric_json FROM qa.rubric_versions WHERE rubric_version" in query:
            e = self.rubrics.get(args[0])
            return {"rubric_json": e["json"]} if e else None
        if "formula_json FROM qa.formula_versions" in query:  # active-for-team
            for e in self.formulas.values():
                if e["team_id"] == args[0] and e["effective_until"] is None:
                    return {"formula_json": e["json"]}
            return None
        raise AssertionError(f"unexpected fetchrow: {query}")

    async def execute(self, query, *args):
        self.executed.append(query.split()[0] + " " + query.split()[2])
        table = "formulas" if "formula_versions" in query else "rubrics"
        store = getattr(self, table)
        if query.startswith("UPDATE"):
            for e in store.values():
                if e["team_id"] == args[0] and e["effective_until"] is None:
                    e["effective_until"] = "now"
        else:  # INSERT (version, team_id, json)
            store[args[0]] = {"team_id": args[1], "json": args[2], "effective_until": None}


@pytest.fixture()
def ms_formula() -> Formula:
    return Formula.model_validate(json.loads(_MS_FORMULA.read_text(encoding="utf-8")))


@pytest.fixture()
def ms_rubric() -> Rubric:
    return get_team_config("member_support").rubric


def _rubric_entry(rubric: Rubric, team="member_support", until=None):
    return {
        "team_id": team,
        "json": json.dumps(rubric.model_dump(by_alias=True, mode="json")),
        "effective_until": until,
    }


class TestShipFormula:
    pytestmark = pytest.mark.asyncio

    async def test_first_ship_inserts_and_closes_prior(self, ms_formula, ms_rubric):
        conn = FakeConn(
            formulas={"member_support_v1_old": {"team_id": "member_support", "json": "{}", "effective_until": None}},
            rubrics={ms_formula.rubric_version: _rubric_entry(ms_rubric)},
        )
        outcome = await ship_formula(conn, "member_support", ms_formula)
        assert outcome.action == "inserted"
        assert conn.formulas["member_support_v1_old"]["effective_until"] == "now"
        assert conn.formulas[ms_formula.formula_id]["effective_until"] is None
        # archived content round-trips through the model unchanged
        assert Formula.model_validate(json.loads(conn.formulas[ms_formula.formula_id]["json"])) == ms_formula

    async def test_reship_same_content_is_noop(self, ms_formula, ms_rubric):
        conn = FakeConn(rubrics={ms_formula.rubric_version: _rubric_entry(ms_rubric)})
        await ship_formula(conn, "member_support", ms_formula)
        executed_before = list(conn.executed)
        outcome = await ship_formula(conn, "member_support", ms_formula)
        assert outcome.action == "unchanged"
        assert conn.executed == executed_before

    async def test_same_version_different_content_refused(self, ms_formula, ms_rubric):
        conn = FakeConn(rubrics={ms_formula.rubric_version: _rubric_entry(ms_rubric)})
        await ship_formula(conn, "member_support", ms_formula)
        mutated = ms_formula.model_dump(by_alias=True)
        mutated["sections"][0]["weight"] = 4.0
        mutated["sections"][1]["weight"] = 6.0
        with pytest.raises(ImmutableVersionError, match="immutable"):
            await ship_formula(conn, "member_support", Formula.model_validate(mutated))

    async def test_missing_rubric_refused(self, ms_formula):
        with pytest.raises(RubricNotArchivedError, match="ship the rubric first"):
            await ship_formula(FakeConn(), "member_support", ms_formula)

    async def test_formula_not_covered_by_rubric_refused(self, ms_formula, ms_rubric):
        pruned = ms_rubric.model_dump(by_alias=True)
        pruned["sections"] = [s for s in pruned["sections"] if s["id"] != "cri"]
        conn = FakeConn(rubrics={
            ms_formula.rubric_version: {
                "team_id": "member_support",
                "json": json.dumps(pruned),
                "effective_until": None,
            }
        })
        with pytest.raises(FormulaRubricMismatchError, match="cri"):
            await ship_formula(conn, "member_support", ms_formula)


class TestShipRubric:
    pytestmark = pytest.mark.asyncio

    async def test_first_ship_inserts_and_closes_prior(self, ms_rubric):
        conn = FakeConn(rubrics={"member_support_v1": {"team_id": "member_support", "json": "{}", "effective_until": None}})
        outcome = await ship_rubric(conn, "member_support", ms_rubric)
        assert outcome.action == "inserted"
        assert conn.rubrics["member_support_v1"]["effective_until"] == "now"
        assert conn.rubrics[ms_rubric.rubric_version]["effective_until"] is None

    async def test_reship_same_content_is_noop(self, ms_rubric):
        conn = FakeConn()
        await ship_rubric(conn, "member_support", ms_rubric)
        outcome = await ship_rubric(conn, "member_support", ms_rubric)
        assert outcome.action == "unchanged"

    async def test_same_version_different_content_refused(self, ms_rubric):
        conn = FakeConn()
        await ship_rubric(conn, "member_support", ms_rubric)
        mutated = ms_rubric.model_dump(by_alias=True)
        mutated["sections"][0]["name"] = "Renamed"
        with pytest.raises(ImmutableVersionError):
            await ship_rubric(conn, "member_support", Rubric.model_validate(mutated))

    async def test_rubric_breaking_active_formula_refused(self, ms_formula, ms_rubric):
        """§3.19.3 — dropping a section the active formula references.
        (`cri` — dropping a scoring_prompt-referenced section like
        process_adherence fails earlier, at Rubric validation.)"""
        conn = FakeConn(formulas={
            ms_formula.formula_id: {
                "team_id": "member_support",
                "json": json.dumps(ms_formula.model_dump(by_alias=True, mode="json")),
                "effective_until": None,
            }
        })
        pruned = ms_rubric.model_dump(by_alias=True)
        pruned["rubric_version"] = "member_support_v3"
        pruned["sections"] = [s for s in pruned["sections"] if s["id"] != "cri"]
        with pytest.raises(FormulaRubricMismatchError, match="cri"):
            await ship_rubric(conn, "member_support", Rubric.model_validate(pruned))

    async def test_no_active_formula_ships_trivially(self, ms_rubric):
        outcome = await ship_rubric(FakeConn(), "member_support", ms_rubric)
        assert outcome.action == "inserted"


class TestStartupShip:

    def test_loads_only_active_formula_files(self):
        """v0_sheet.json (historic archive, ships via backfill B1) must never
        be picked up by the startup path — it would close the active version."""
        formulas = load_active_formula_files()
        assert "member_support" in formulas
        assert all(f.formula_id != "member_support_v0_sheet" for f in formulas.values())

    @pytest.mark.asyncio
    async def test_no_database_url_validates_and_skips(self):
        assert await run_startup_ship(None) == []

    @pytest.mark.asyncio
    async def test_bad_formula_file_raises_even_without_db(self, tmp_path):
        team = tmp_path / "member_support"
        team.mkdir()
        (team / "overall_formula.json").write_text('{"formula_id": "broken"}')
        with pytest.raises(Exception):
            load_active_formula_files(tmp_path)

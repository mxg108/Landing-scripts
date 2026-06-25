"""Tests for the test scaffolding itself (§11.0 fixtures).

Verifies the ``pg_tx`` SAVEPOINT-rolled-back pattern actually isolates
writes across tests, and that the golden-fixture loader returns the
right shape for both present and missing fixture files.

These are tests-of-tests, so they don't carry per-§11.5 floor coverage
(no migration to validate). They exist so a future contributor breaking
the fixture isolation is caught at PR time.
"""

from __future__ import annotations

import sys
from pathlib import Path

import asyncpg
import pytest

# conftest.py is loaded by pytest but not importable by name. Add this
# directory to sys.path so we can `import conftest` like a regular module.
_INTEGRATION_DIR = Path(__file__).resolve().parent
if str(_INTEGRATION_DIR) not in sys.path:
    sys.path.insert(0, str(_INTEGRATION_DIR))

import conftest as _conftest  # noqa: E402
from conftest import EPSILON, load_overall_formula  # noqa: E402


# ---------------------------------------------------------------------------
# pg_tx isolation — writes in one test must not be visible to the next
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pg_tx_writes_visible_within_same_test(
    pg_tx: asyncpg.Connection,
) -> None:
    """Inside one test, writes ARE visible (the rollback only happens at
    test exit). This is the baseline — without it the SAVEPOINT pattern
    wouldn't work for any test that needs to observe its own state."""
    await pg_tx.execute(
        "INSERT INTO qa.agents (team_id, name, email) "
        "VALUES ('sales', 'Tx Probe Alpha', 'p@l.com')"
    )
    n = await pg_tx.fetchval(
        "SELECT COUNT(*) FROM qa.agents WHERE name = 'Tx Probe Alpha'"
    )
    assert n == 1


@pytest.mark.asyncio
async def test_pg_tx_isolation_part_1_writes_probe_row(
    pg_tx: asyncpg.Connection,
) -> None:
    """Part 1 of an isolation pair: write a row. The test passes; on
    exit the transaction rolls back. Part 2 (below) must then see no
    such row — proving the isolation works across tests."""
    await pg_tx.execute(
        "INSERT INTO qa.agents (team_id, name, email) "
        "VALUES ('sales', 'Tx Isolation Probe', 'p@l.com')"
    )


@pytest.mark.asyncio
async def test_pg_tx_isolation_part_2_does_not_see_part_1(
    pg_tx: asyncpg.Connection,
) -> None:
    """Part 2 of the isolation pair. If pg_tx is truly SAVEPOINT-rolled-
    back, this test starts in the migrated-but-empty state — the row
    inserted in part 1 must not be visible."""
    n = await pg_tx.fetchval(
        "SELECT COUNT(*) FROM qa.agents WHERE name = 'Tx Isolation Probe'"
    )
    assert n == 0


@pytest.mark.asyncio
async def test_pg_tx_inherits_seed_rows_from_004(
    pg_tx: asyncpg.Connection,
) -> None:
    """The session-baseline includes 004's seed (member_support, sales).
    pg_tx tests can rely on those rows being present without inserting
    them."""
    rows = await pg_tx.fetch("SELECT id FROM public.teams ORDER BY id")
    assert [r["id"] for r in rows] == ["member_support", "sales"]


# ---------------------------------------------------------------------------
# load_overall_formula — present + missing fixture behavior
# ---------------------------------------------------------------------------


def test_load_overall_formula_missing_returns_empty() -> None:
    """No fixture file exists yet for either team. The loader returns
    an empty list rather than raising — that lets parametrized golden-
    fixture tests stay registerable before fixtures are authored."""
    assert load_overall_formula("member_support") == []
    assert load_overall_formula("sales") == []


def test_load_overall_formula_present_returns_rows(tmp_path, monkeypatch) -> None:
    """Authoring a fixture: drop a JSON with the expected ``rows``
    array, the loader returns it. Future Wave-2 PRs that add real
    fixtures rely on this shape contract."""
    import conftest
    fixtures_dir = tmp_path / "overall_formula"
    fixtures_dir.mkdir()
    payload = {
        "rows": [
            {"evaluation_id": 1, "sections": [], "expected_score": 87.5},
            {"evaluation_id": 2, "sections": [], "expected_score": 92.1},
        ],
    }
    (fixtures_dir / "sales.json").write_text(__import__("json").dumps(payload))
    monkeypatch.setattr(conftest, "_FORMULA_FIXTURES_DIR", fixtures_dir)
    rows = conftest.load_overall_formula("sales")
    assert len(rows) == 2
    assert rows[0]["expected_score"] == 87.5


def test_epsilon_pinned_at_005() -> None:
    """The Phase A.5 acceptance threshold (§3.6) is exactly 0.05 — pin
    it here so accidental changes show up as a failing test, not a
    silently shifted compliance gate."""
    assert EPSILON == 0.05

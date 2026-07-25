"""rebuild_agent_series — ScorecardActionsDesign §5 (slice S2).

The action-time rebuild (rescore / override / delete) and the F1 replay
CLI share `build_series`, so parity with scripts/backfill_stat_points.py
is structural. What these tests pin is the rebuild's DB contract: fetch
order, delete-before-insert, write fidelity, the mid-chain invalidation
the manual 2026-07-24 delete couldn't handle, and the fatal-by-contract
error path.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.services.stat_points import (
    COVERAGE_REGIME,
    build_series,
    rebuild_agent_series,
)


def _config(span=5, k=2.0):
    return SimpleNamespace(
        stats=SimpleNamespace(ewma_span=span, spc_sigma_multiplier=k)
    )


def _evals(scores):
    return [
        {"id": i, "team_id": "member_support", "overall_score": s}
        for i, s in enumerate(scores, start=1)
    ]


class FakeConn:
    """Captures the rebuild's operation order and INSERT payloads."""

    def __init__(self, evals, fail_on_insert=False):
        self.evals = evals
        self.fail_on_insert = fail_on_insert
        self.ops: list[str] = []
        self.inserts: list[tuple] = []

    async def fetch(self, query, *args):
        self.ops.append("fetch")
        assert "state = 'finalized'" in query
        assert "ORDER BY approved_at, id" in query
        return self.evals

    async def execute(self, query, *args):
        if query.startswith("DELETE"):
            self.ops.append("delete")
            return "DELETE 3"
        assert query.startswith("INSERT INTO qa.agent_stat_points")
        if self.fail_on_insert:
            raise RuntimeError("boom")
        self.ops.append("insert")
        self.inserts.append(args)
        return "INSERT 0 1"


pytestmark = pytest.mark.asyncio


async def test_rebuild_writes_full_series_delete_first():
    scores = [80.0, 92.0, 75.0, 88.0]
    conn = FakeConn(_evals(scores))
    written = await rebuild_agent_series(conn, agent_id=7, config=_config())

    assert written == 4
    assert conn.ops == ["fetch", "delete", "insert", "insert", "insert", "insert"]

    expected = build_series(_evals(scores), span=5, sigma_multiplier=2.0)
    for args, exp in zip(conn.inserts, expected):
        team_id, agent_id, evaluation_id, score, ewma, lam, mean, sigma, flags, regime = args
        assert (team_id, agent_id) == ("member_support", 7)
        assert evaluation_id == exp["evaluation_id"]
        assert score == exp["score"]
        assert ewma == exp["point"].ewma
        assert lam == exp["point"].ewma_lambda
        assert mean == exp["point"].spc_mean
        assert sigma == exp["point"].spc_sigma
        assert flags == exp["point"].spc_flags
        assert regime == COVERAGE_REGIME


async def test_mid_chain_removal_recomputes_downstream():
    """The 2026-07-24 manual delete got lucky (latest point). The rebuild
    must handle the unlucky case: removing a mid-chain eval changes every
    downstream EWMA/σ and re-judges flags against the new control limits."""
    full_scores = [85.0, 86.0, 84.0, 85.0, 40.0]
    full = build_series(_evals(full_scores), span=5, sigma_multiplier=2.0)
    # The 40.0 crash is flagged in the intact series (pinned in
    # test_stat_points); removing eval 2 (86.0) must not change that
    # verdict but must shift the whole chain's values.
    assert full[4]["point"].spc_flags == ["below_lcl"]

    remaining = [
        {"id": i, "team_id": "member_support", "overall_score": s}
        for i, s in [(1, 85.0), (3, 84.0), (4, 85.0), (5, 40.0)]
    ]
    conn = FakeConn(remaining)
    written = await rebuild_agent_series(conn, agent_id=7, config=_config())

    assert written == 4
    rebuilt = build_series(remaining, span=5, sigma_multiplier=2.0)
    # Downstream of the removal, the chain genuinely differs from the
    # original series' corresponding points...
    assert rebuilt[1]["point"].ewma != full[2]["point"].ewma
    assert rebuilt[3]["point"].spc_mean != full[4]["point"].spc_mean
    # ...and what lands in the DB is exactly the recomputed chain.
    assert [a[4] for a in conn.inserts] == [r["point"].ewma for r in rebuilt]
    assert conn.inserts[3][8] == rebuilt[3]["point"].spc_flags


async def test_empty_history_still_clears_stale_points():
    """An agent whose last finalized eval was deleted keeps zero points —
    the DELETE must run even when there is nothing to re-insert."""
    conn = FakeConn([])
    written = await rebuild_agent_series(conn, agent_id=7, config=_config())
    assert written == 0
    assert conn.ops == ["fetch", "delete"]


async def test_rebuild_is_fatal_by_contract():
    """Unlike write_point_for_finalize (non-fatal), a rebuild failure must
    propagate so the caller's surrounding transaction rolls back."""
    conn = FakeConn(_evals([80.0, 90.0]), fail_on_insert=True)
    with pytest.raises(RuntimeError, match="boom"):
        await rebuild_agent_series(conn, agent_id=7, config=_config())

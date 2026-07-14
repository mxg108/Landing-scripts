"""qa.agent_stat_points math — SQLMigration §9.2 / ReadPathFlip F1.

Pins the incremental EWMA/Welford/SPC-flag computations and the replay
series builder. The DB writer paths are exercised by the replay script's
dry-run + report against real data.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pytest

from backend.services.stat_points import (
    COVERAGE_REGIME,
    PriorState,
    compute_point,
    lambda_from_span,
)

_REPLAY = Path(__file__).resolve().parent.parent / "scripts" / "backfill_stat_points.py"
spec = importlib.util.spec_from_file_location("backfill_stat_points", _REPLAY)
replay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(replay)


def series(scores, span=5, k=2.0):
    return replay.build_series(
        [{"id": i, "overall_score": s} for i, s in enumerate(scores, start=1)],
        span=span, sigma_multiplier=k)


def test_lambda_from_span_matches_pandas_convention():
    assert lambda_from_span(5) == pytest.approx(2 / 6, abs=0.001)
    assert lambda_from_span(9) == 0.2


def test_first_point_seeds_ewma_with_score():
    [r] = series([87.0])
    assert r["point"].ewma == 87.0
    assert r["point"].spc_mean == 87.0
    assert r["point"].spc_sigma is None       # σ undefined at n=1
    assert r["point"].spc_flags == []


def test_ewma_recursion():
    rows = series([80.0, 92.0], span=5)
    lam = lambda_from_span(5)
    expected = lam * 92.0 + (1 - lam) * 80.0
    assert rows[1]["point"].ewma == pytest.approx(expected, abs=0.01)


def test_running_mean_sigma_match_numpy():
    scores = [80.0, 92.0, 75.0, 88.0, 97.0, 61.0]
    rows = series(scores)
    for i in range(1, len(scores)):
        prefix = np.array(scores[: i + 1])
        assert rows[i]["point"].spc_mean == pytest.approx(prefix.mean(), abs=0.005)
        assert rows[i]["point"].spc_sigma == pytest.approx(
            prefix.std(ddof=1), abs=0.0005)


def test_spc_flags_judged_against_prior_stats():
    # Stable history, then a crash: flagged via the PRIOR limits.
    scores = [85.0, 86.0, 84.0, 85.0, 40.0]
    rows = series(scores, k=2.0)
    assert rows[4]["point"].spc_flags == ["below_lcl"]
    # And the spike direction:
    rows_up = series([85.0, 86.0, 84.0, 85.0, 99.0], k=2.0)
    assert rows_up[4]["point"].spc_flags == ["above_ucl"]
    # In-band point: no flags.
    assert rows[3]["point"].spc_flags == []


def test_no_flags_before_sigma_defined_or_when_zero():
    # n=2: prior σ undefined at point 2 → no flag even for a wild jump.
    rows = series([85.0, 20.0])
    assert rows[1]["point"].spc_flags == []
    # Identical history → σ=0 → guarded, no flags.
    rows_flat = series([85.0, 85.0, 85.0, 40.0])
    assert rows_flat[3]["point"].spc_flags == []


def test_series_is_deterministic_and_order_sensitive():
    a = series([80.0, 90.0, 85.0])
    b = series([80.0, 90.0, 85.0])
    assert [r["point"] for r in a] == [r["point"] for r in b]
    c = series([85.0, 90.0, 80.0])
    assert a[2]["point"].ewma != c[2]["point"].ewma  # order matters (EWMA)


def test_prior_state_threading_matches_full_series():
    """Incremental (finalize-time) computation must equal the replay's
    full-series computation — same math, two entry points."""
    scores = [80.0, 92.0, 75.0, 88.0]
    full = series(scores)
    prior = PriorState()
    for i, s in enumerate(scores):
        p = compute_point(s, prior, span=5, sigma_multiplier=2.0)
        assert p == full[i]["point"]
        prior = PriorState(n=prior.n + 1, ewma=p.ewma,
                           mean=p.spc_mean, sigma=p.spc_sigma)


def test_coverage_regime_constant():
    assert COVERAGE_REGIME == "manager_sample"  # predictive-groundwork freeze

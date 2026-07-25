#!/usr/bin/env python3
"""F1 replay — rebuild qa.agent_stat_points from finalized history.

SQLMigration §9.2 / ReadPathFlip F1: replays every finalized evaluation
with a resolved agent identity in **approved_at order** (id tiebreak)
and recomputes each agent's incremental EWMA/SPC series from scratch.

Rebuild, not gap-fill, on purpose: points written live before this
replay saw an empty (or partial) table as their prior state, so their
EWMA/σ are wrong the moment earlier history lands. The series is
deterministic, so DELETE + re-INSERT per agent (one transaction each)
is both correct and idempotent — run it any number of times.

Evaluations with agent_id IS NULL (departed agents, plan §7) have no
sparkline consumer and are counted, not written.

Usage:
    cd qa-automation/AI-Scoring
    python3 scripts/backfill_stat_points.py --team-id member_support --dry-run
    python3 scripts/backfill_stat_points.py --team-id member_support
    python3 scripts/backfill_stat_points.py --team-id sales
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv

_AI_SCORING = Path(__file__).resolve().parent.parent
_STAGING_DIR = _AI_SCORING.parent.parent / "database" / "backfill_staging"
load_dotenv(_AI_SCORING / ".env")

if str(_AI_SCORING) not in sys.path:
    sys.path.insert(0, str(_AI_SCORING))

from backend.config.team_config import get_team_config  # noqa: E402
from backend.services.stat_points import (  # noqa: E402
    COVERAGE_REGIME,
    build_series,
)

_b1_spec = importlib.util.spec_from_file_location(
    "backfill_seed_b1", Path(__file__).resolve().parent / "backfill_seed_b1.py")
b1 = importlib.util.module_from_spec(_b1_spec)
_b1_spec.loader.exec_module(b1)


# build_series moved to backend.services.stat_points (ScorecardActionsDesign
# §5) — the action-time rebuild (rescore/override/delete) and this replay
# CLI share one math core.


async def run(args) -> int:
    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn:
        print("structural: DATABASE_URL not set")
        return 1
    config = get_team_config(args.team_id)
    span = config.stats.ewma_span
    k = config.stats.spc_sigma_multiplier

    db = b1.DbSession(dsn)
    await db.connect()
    counts = {"finalized_evals": 0, "no_agent_id": 0, "agents": 0,
              "points_written": 0, "points_deleted": 0, "flagged_points": 0}
    try:
        evals = await b1.with_reconnect(db, lambda: db.conn.fetch(
            "SELECT id, agent_id, overall_score, approved_at "
            "FROM qa.evaluations "
            "WHERE team_id = $1 AND state = 'finalized' "
            "ORDER BY approved_at, id", args.team_id))
        counts["finalized_evals"] = len(evals)
        by_agent: dict[int, list] = defaultdict(list)
        for ev in evals:
            if ev["agent_id"] is None:
                counts["no_agent_id"] += 1
            else:
                by_agent[ev["agent_id"]].append(ev)
        counts["agents"] = len(by_agent)

        for agent_id, agent_evals in sorted(by_agent.items()):
            series = build_series(agent_evals, span=span, sigma_multiplier=k)
            counts["flagged_points"] += sum(
                1 for r in series if r["point"].spc_flags)
            if args.dry_run:
                counts["points_written"] += len(series)
                continue

            async def rebuild(agent_id=agent_id, series=series):
                async with db.conn.transaction():
                    deleted = await db.conn.execute(
                        "DELETE FROM qa.agent_stat_points WHERE agent_id = $1",
                        agent_id)
                    await db.conn.executemany(
                        "INSERT INTO qa.agent_stat_points "
                        "(team_id, agent_id, evaluation_id, score, ewma, "
                        " ewma_lambda, spc_mean, spc_sigma, spc_flags, coverage_regime) "
                        "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)",
                        [(args.team_id, agent_id, r["evaluation_id"], r["score"],
                          r["point"].ewma, r["point"].ewma_lambda,
                          r["point"].spc_mean, r["point"].spc_sigma,
                          r["point"].spc_flags, COVERAGE_REGIME)
                         for r in series])
                    return int(deleted.split()[-1])

            counts["points_deleted"] += await b1.with_reconnect(db, rebuild)
            counts["points_written"] += len(series)
    finally:
        await db.close()

    report = {"stage": "F1-replay", "team_id": args.team_id,
              "dry_run": args.dry_run, "ewma_span": span,
              "sigma_multiplier": k, "counts": counts}
    report_path = _STAGING_DIR / f"report_{args.team_id}_stat_points.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    tag = " [DRY RUN]" if args.dry_run else ""
    print(f"[F1:{args.team_id}]{tag} evals={counts['finalized_evals']} "
          f"agents={counts['agents']} points={counts['points_written']} "
          f"(deleted {counts['points_deleted']} stale) "
          f"no-agent skipped={counts['no_agent_id']} "
          f"spc-flagged={counts['flagged_points']}")
    print(f"  report: {report_path}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--team-id", required=True, choices=sorted(b1.FORMULA_VERSION))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())

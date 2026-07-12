#!/usr/bin/env python3
"""B4 — backfill verification + ε-sweep handoff (BackfillPlan §5, §7.4).

Read-only. Compares the database's backfilled rows against the B0
staging file (the reviewed source of truth) across every §7.4 axis:

  - row + section counts (permanently-blocked rows excluded)
  - overall-score sum, exact
  - per-month row counts + score means (bucketed by approved_at — the
    clock B2's authoritative-overwrite never touches)
  - per-agent row counts
  - anomaly annotations present (the §2/§2a hand-edit rows)
  - v0_sheet formula rows archived; natural keys unique
  - identity coverage after B3 (informational)

Exit 0 = every check green (ε sweep may proceed — attach the §2
narrative before showing leadership any deltas); 2 = drift, read the
report before going further.

Usage:
    cd qa-automation/AI-Scoring
    python3 scripts/backfill_seed_b4.py --team-id member_support
    python3 scripts/backfill_seed_b4.py --team-id sales
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

from dotenv import load_dotenv

_AI_SCORING = Path(__file__).resolve().parent.parent
_STAGING_DIR = _AI_SCORING.parent.parent / "database" / "backfill_staging"
load_dotenv(_AI_SCORING / ".env")

_b1_spec = importlib.util.spec_from_file_location(
    "backfill_seed_b1", Path(__file__).resolve().parent / "backfill_seed_b1.py")
b1 = importlib.util.module_from_spec(_b1_spec)
_b1_spec.loader.exec_module(b1)

_NK = "dialpad_call_metadata #>> '{backfill,natural_key}'"
_ANOM = "dialpad_call_metadata #>> '{backfill,backfill_anomaly}'"


def staging_aggregates(staged: list[dict]) -> dict:
    """Expected values from the reviewed B0 staging file."""
    months = Counter()
    month_sums: dict[str, float] = defaultdict(float)
    agents = Counter()
    anomalies = 0
    total_sum = 0.0
    for s in staged:
        month = (s["approved_at"] or "")[:7]
        months[month] += 1
        month_sums[month] += s["overall_score"]
        agents[s["agent_name_raw"].strip().lower()] += 1
        total_sum += s["overall_score"]
        if "backfill_anomaly" in s["annotations"]:
            anomalies += 1
    return {
        "rows": len(staged),
        "sections": sum(len(s["sections"]) for s in staged),
        "score_sum": round(total_sum, 1),
        "months": dict(months),
        "month_means": {m: round(month_sums[m] / n, 2) for m, n in months.items()},
        "agents": dict(agents),
        "anomalies": anomalies,
    }


async def db_aggregates(db, team_id: str) -> dict:
    row = await b1.with_reconnect(db, lambda: db.conn.fetchrow(
        f"SELECT COUNT(*) AS rows, COALESCE(SUM(overall_score),0) AS score_sum, "
        f"COUNT(*) FILTER (WHERE {_ANOM} IS NOT NULL) AS anomalies, "
        f"COUNT(*) FILTER (WHERE agent_id IS NOT NULL) AS with_agent_id, "
        f"COUNT(DISTINCT {_NK}) AS distinct_keys "
        f"FROM qa.evaluations WHERE team_id = $1 AND {_NK} IS NOT NULL", team_id))
    sections = await b1.with_reconnect(db, lambda: db.conn.fetchval(
        f"SELECT COUNT(*) FROM qa.evaluation_sections es "
        f"JOIN qa.evaluations e ON e.id = es.evaluation_id "
        f"WHERE e.team_id = $1 AND e.{_NK} IS NOT NULL", team_id))
    months = await b1.with_reconnect(db, lambda: db.conn.fetch(
        f"SELECT to_char(approved_at AT TIME ZONE 'UTC', 'YYYY-MM') AS m, "
        f"COUNT(*) AS n, ROUND(AVG(overall_score), 2) AS mean "
        f"FROM qa.evaluations WHERE team_id = $1 AND {_NK} IS NOT NULL "
        f"GROUP BY 1", team_id))
    agents = await b1.with_reconnect(db, lambda: db.conn.fetch(
        f"SELECT LOWER(TRIM(agent_name_raw)) AS a, COUNT(*) AS n "
        f"FROM qa.evaluations WHERE team_id = $1 AND {_NK} IS NOT NULL "
        f"GROUP BY 1", team_id))
    formula = await b1.with_reconnect(db, lambda: db.conn.fetchval(
        "SELECT 1 FROM qa.formula_versions WHERE formula_version = $1",
        b1.FORMULA_VERSION[team_id]))
    return {
        "rows": row["rows"], "sections": sections,
        "score_sum": round(float(row["score_sum"]), 1),
        "anomalies": row["anomalies"],
        "with_agent_id": row["with_agent_id"],
        "distinct_keys": row["distinct_keys"],
        "months": {r["m"]: r["n"] for r in months},
        "month_means": {r["m"]: float(r["mean"]) for r in months},
        "agents": {r["a"]: r["n"] for r in agents},
        "formula_archived": bool(formula),
    }


async def run(args) -> int:
    staging_path = _STAGING_DIR / f"staging_{args.team_id}.jsonl"
    if not staging_path.exists():
        print(f"structural: staging file missing: {staging_path}")
        return 1
    staged = [json.loads(line) for line in staging_path.open(encoding="utf-8")]
    importable = [s for s in staged if not s["import_blocked"]]
    expected = staging_aggregates(importable)

    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn:
        print("structural: DATABASE_URL not set")
        return 1
    db = b1.DbSession(dsn)
    await db.connect()
    try:
        actual = await db_aggregates(db, args.team_id)
    finally:
        await db.close()

    month_drift = {m: (expected["months"].get(m), actual["months"].get(m))
                   for m in set(expected["months"]) | set(actual["months"])
                   if expected["months"].get(m) != actual["months"].get(m)}
    mean_drift = {m: (expected["month_means"].get(m), actual["month_means"].get(m))
                  for m in expected["month_means"]
                  if abs((expected["month_means"].get(m) or 0)
                         - (actual["month_means"].get(m) or 0)) > 0.01}
    agent_drift = {a: (expected["agents"].get(a), actual["agents"].get(a))
                   for a in set(expected["agents"]) | set(actual["agents"])
                   if expected["agents"].get(a) != actual["agents"].get(a)}

    checks = {
        "rows": actual["rows"] == expected["rows"],
        "sections": actual["sections"] == expected["sections"],
        "score_sum_exact": actual["score_sum"] == expected["score_sum"],
        "natural_keys_unique": actual["distinct_keys"] == actual["rows"],
        "anomalies_annotated": actual["anomalies"] == expected["anomalies"],
        "per_month_counts": not month_drift,
        "per_month_means": not mean_drift,
        "per_agent_counts": not agent_drift,
        "v0_sheet_formula_archived": actual["formula_archived"],
    }
    report = {
        "stage": "B4", "team_id": args.team_id, "checks": checks,
        "expected": {k: v for k, v in expected.items() if k not in ("agents",)},
        "actual": {k: v for k, v in actual.items() if k not in ("agents",)},
        "drift": {"months": month_drift, "month_means": mean_drift,
                  "agents": agent_drift},
        "identity_coverage": f"{actual['with_agent_id']}/{actual['rows']}",
        "epsilon_sweep_handoff": (
            "Backfill verified — the Phase 6 sweep may run compute_overall_score() "
            "under the current formula. Attach the BackfillPlan §2/§2a narrative "
            "before presenting deltas: large deltas are BY DESIGN (the sheet "
            "ignored sections the new formulas weight heavily)."),
    }
    report_path = _STAGING_DIR / f"report_{args.team_id}_b4.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"[B4:{args.team_id}] rows {actual['rows']}/{expected['rows']} | "
          f"sections {actual['sections']}/{expected['sections']} | "
          f"score_sum {actual['score_sum']} vs {expected['score_sum']} | "
          f"anomalies {actual['anomalies']}/{expected['anomalies']}")
    print(f"  identity coverage (post-B3): {report['identity_coverage']}")
    for name, ok in checks.items():
        print(f"  check {name}: {'OK' if ok else 'DRIFTED'}")
    print(f"  report: {report_path}")
    return 0 if all(checks.values()) else 2


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--team-id", required=True, choices=sorted(b1.FORMULA_VERSION))
    args = ap.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())

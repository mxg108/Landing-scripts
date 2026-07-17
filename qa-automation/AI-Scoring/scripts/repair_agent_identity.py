#!/usr/bin/env python3
"""Repair agent_id on identity-orphaned evaluations (any origin).

Evals finalized while the agent's qa.agents row was stale/missing carry
``agent_id IS NULL`` forever — the finalize-time identity stamp is never
revisited, and a roster re-import can't retro-stamp (observed 2026-07-15:
26 orphaned member_support rows undercounting the dashboard roster,
PR #113). The READ paths self-heal via the read-time roster fallback;
this script repairs the column itself for the write-side consumers
(qa.assessments FK, stat points, future CC joins).

Same Stage-4 matching semantics as the finalize stamp and B3
(backfill_seed_b3.py): ACTIVE roster rows, case-insensitive on name OR
canonical_name. Names the active roster doesn't know stay NULL —
departed agents keep the §7 treatment; run import_agents.py first so the
roster is current.

Unlike B3 this covers ALL rows (not just backfilled ones) and any state —
a draft stamped early is simply ahead of its finalize. Idempotent: only
touches rows where agent_id IS NULL.

Usage:
    cd qa-automation/AI-Scoring
    python3 scripts/repair_agent_identity.py --team-id member_support --dry-run
    python3 scripts/repair_agent_identity.py --team-id member_support
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

_AI_SCORING = Path(__file__).resolve().parent.parent
_STAGING_DIR = _AI_SCORING.parent.parent / "database" / "backfill_staging"
load_dotenv(_AI_SCORING / ".env")

# Stage-4 / B3 matching semantics — keep in lockstep with
# eval_store.finalize_evaluation_scoring's identity UPDATE.
_MATCH = ("a.team_id = e.team_id AND a.active "
          "AND (LOWER(a.name) = LOWER(e.agent_name_raw) "
          "     OR LOWER(a.canonical_name) = LOWER(e.agent_name_raw))")


async def run(args) -> int:
    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn:
        print("DATABASE_URL not set", file=sys.stderr)
        return 2

    import asyncpg

    conn = await asyncpg.connect(dsn, timeout=10)
    try:
        before = await conn.fetchrow(
            "SELECT COUNT(*) AS total, "
            "COUNT(*) FILTER (WHERE agent_id IS NULL) AS unresolved, "
            "COUNT(*) FILTER (WHERE agent_email IS NULL) AS no_email "
            "FROM qa.evaluations e WHERE team_id = $1", args.team_id)

        resolvable = await conn.fetchval(
            f"SELECT COUNT(*) FROM qa.evaluations e "
            f"WHERE e.team_id = $1 AND e.agent_id IS NULL "
            f"AND EXISTS (SELECT 1 FROM qa.agents a WHERE {_MATCH})",
            args.team_id)

        if not args.dry_run and resolvable:
            await conn.execute(
                f"UPDATE qa.evaluations e SET "
                f"agent_id = a.id, "
                f"agent_email = COALESCE(e.agent_email, a.email) "
                f"FROM qa.agents a "
                f"WHERE e.team_id = $1 AND e.agent_id IS NULL AND {_MATCH}",
                args.team_id)

        after = await conn.fetchrow(
            "SELECT COUNT(*) FILTER (WHERE agent_id IS NULL) AS unresolved, "
            "COUNT(*) FILTER (WHERE agent_email IS NULL) AS no_email "
            "FROM qa.evaluations e WHERE team_id = $1", args.team_id)

        # The stays-NULL list, explicit (departed agents — B3 §7 treatment).
        unresolved_names = await conn.fetch(
            "SELECT agent_name_raw, COUNT(*) AS n FROM qa.evaluations e "
            "WHERE team_id = $1 AND agent_id IS NULL "
            "GROUP BY 1 ORDER BY n DESC", args.team_id)
    finally:
        await conn.close()

    report = {
        "stage": "identity_repair", "team_id": args.team_id,
        "dry_run": args.dry_run,
        "counts": {
            "rows": before["total"],
            "unresolved_before": before["unresolved"],
            "resolvable": resolvable,
            "unresolved_after": after["unresolved"],
            "resolved_this_run": before["unresolved"] - after["unresolved"],
            "email_missing_before": before["no_email"],
            "email_missing_after": after["no_email"],
        },
        "unresolved_names": [{"name": r["agent_name_raw"], "rows": r["n"]}
                             for r in unresolved_names],
    }
    report_path = _STAGING_DIR / f"report_{args.team_id}_identity_repair.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    c = report["counts"]
    tag = " [DRY RUN]" if args.dry_run else ""
    print(f"[identity-repair:{args.team_id}]{tag} rows={c['rows']} "
          f"unresolved={c['unresolved_before']} resolvable={resolvable} "
          f"resolved={c['resolved_this_run']}")
    print(f"  remaining NULL: {c['unresolved_after']} rows across "
          f"{len(report['unresolved_names'])} names (departed — stay NULL)")
    for r in report["unresolved_names"][:10]:
        print(f"    {r['rows']:4d}  {r['name']}")
    if len(report["unresolved_names"]) > 10:
        print(f"    ... +{len(report['unresolved_names']) - 10} more (see report)")
    print(f"  report: {report_path}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--team-id", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())

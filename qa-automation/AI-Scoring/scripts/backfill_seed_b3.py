#!/usr/bin/env python3
"""B3 — agent identity resolution for backfilled rows (BackfillPlan §5).

One set-based pass after B2 settles: resolve ``agent_id`` via qa.agents
using exactly the Stage-4 matching semantics (active roster rows,
case-insensitive on name OR canonical_name), and repair missing historic
``agent_email`` where the roster covers it. Names the roster doesn't
know stay NULL forever — accepted per plan §7, and reported here so the
list is explicit, not implicit.

Idempotent: only touches backfill rows where ``agent_id IS NULL``.

Usage:
    cd qa-automation/AI-Scoring
    python3 scripts/backfill_seed_b3.py --team-id member_support --dry-run
    python3 scripts/backfill_seed_b3.py --team-id member_support
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import sys
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

_MATCH = ("a.team_id = e.team_id AND a.active "
          "AND (LOWER(a.name) = LOWER(e.agent_name_raw) "
          "     OR LOWER(a.canonical_name) = LOWER(e.agent_name_raw))")


async def run(args) -> int:
    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn:
        print("structural: DATABASE_URL not set")
        return 1
    db = b1.DbSession(dsn)
    await db.connect()
    try:
        before = await b1.with_reconnect(db, lambda: db.conn.fetchrow(
            f"SELECT COUNT(*) AS total, "
            f"COUNT(*) FILTER (WHERE agent_id IS NULL) AS unresolved, "
            f"COUNT(*) FILTER (WHERE agent_email IS NULL) AS no_email "
            f"FROM qa.evaluations e "
            f"WHERE team_id = $1 AND {_NK} IS NOT NULL", args.team_id))

        resolvable = await b1.with_reconnect(db, lambda: db.conn.fetchval(
            f"SELECT COUNT(*) FROM qa.evaluations e "
            f"WHERE e.team_id = $1 AND {_NK} IS NOT NULL AND e.agent_id IS NULL "
            f"AND EXISTS (SELECT 1 FROM qa.agents a WHERE {_MATCH})", args.team_id))

        if not args.dry_run and resolvable:
            await b1.with_reconnect(db, lambda: db.conn.execute(
                f"UPDATE qa.evaluations e SET "
                f"agent_id = a.id, "
                f"agent_email = COALESCE(e.agent_email, a.email) "
                f"FROM qa.agents a "
                f"WHERE e.team_id = $1 AND e.{_NK} IS NOT NULL "
                f"AND e.agent_id IS NULL AND {_MATCH}", args.team_id))

        after = await b1.with_reconnect(db, lambda: db.conn.fetchrow(
            f"SELECT COUNT(*) FILTER (WHERE agent_id IS NULL) AS unresolved, "
            f"COUNT(*) FILTER (WHERE agent_email IS NULL) AS no_email "
            f"FROM qa.evaluations e "
            f"WHERE team_id = $1 AND {_NK} IS NOT NULL", args.team_id))

        # The accepted-NULL-forever list, made explicit (plan §7).
        unresolved_names = await b1.with_reconnect(db, lambda: db.conn.fetch(
            f"SELECT agent_name_raw, COUNT(*) AS n FROM qa.evaluations e "
            f"WHERE team_id = $1 AND {_NK} IS NOT NULL AND agent_id IS NULL "
            f"GROUP BY 1 ORDER BY n DESC", args.team_id))
    finally:
        await db.close()

    report = {
        "stage": "B3", "team_id": args.team_id, "dry_run": args.dry_run,
        "counts": {
            "backfill_rows": before["total"],
            "unresolved_before": before["unresolved"],
            "resolvable": resolvable,
            "unresolved_after": after["unresolved"],
            "resolved_this_run": before["unresolved"] - after["unresolved"],
            "email_missing_before": before["no_email"],
            "email_missing_after": after["no_email"],
            "emails_repaired": before["no_email"] - after["no_email"],
        },
        "unresolved_names": [{"name": r["agent_name_raw"], "rows": r["n"]}
                             for r in unresolved_names],
    }
    report_path = _STAGING_DIR / f"report_{args.team_id}_b3.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    c = report["counts"]
    tag = " [DRY RUN]" if args.dry_run else ""
    print(f"[B3:{args.team_id}]{tag} rows={c['backfill_rows']} "
          f"resolved={c['resolved_this_run']} (resolvable={c['resolvable']}) "
          f"emails repaired={c['emails_repaired']}")
    print(f"  remaining unresolved: {c['unresolved_after']} rows across "
          f"{len(report['unresolved_names'])} names (NULL forever — plan §7)")
    for r in report["unresolved_names"][:10]:
        print(f"    {r['rows']:4d}  {r['name']}")
    if len(report["unresolved_names"]) > 10:
        print(f"    ... +{len(report['unresolved_names']) - 10} more (see report)")
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

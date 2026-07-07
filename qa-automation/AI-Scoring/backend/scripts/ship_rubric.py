"""Ship a team's rubric into qa.rubric_versions — §3.19.2 programmatic path.

The first step of the version-ship ceremony (see services/version_ship.py):
the rubric must be archived before the formula referencing it can ship at
FastAPI startup.

Usage:
    cd qa-automation/AI-Scoring
    python -m backend.scripts.ship_rubric --team member_support [--dry-run]

Reads the rubric from the team's config JSON (the same normalized shape the
app loads), validates the §3.19.3 invariant against the team's active
formula, and inserts a new qa.rubric_versions row, closing the prior active
one in the same transaction. Idempotent: re-running with unchanged content
is a no-op; changed content under an already-shipped version id is refused
(bump rubric_version instead).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

from backend.config.team_config import get_team_config  # noqa: E402
from backend.models.formula import Rubric  # noqa: E402
from backend.services.version_ship import ship_rubric  # noqa: E402


async def _run(team_id: str, dry_run: bool, file: str | None = None) -> int:
    if file:
        # Ship a staged rubric file (e.g. config/scoring/sales/rubric_v2.json)
        # BEFORE the runtime team config migrates to it — the sales_v2 flow:
        # archive the rubric first, the formula ships at the next restart,
        # the team-config/pipeline swap follows in the flip bundle.
        import json as _json
        rubric = Rubric.model_validate(_json.loads(Path(file).read_text(encoding="utf-8")))
    else:
        config = get_team_config(team_id)  # validates the rubric shape on load
        rubric = config.rubric
    print(f"team={team_id} rubric_version={rubric.rubric_version} "
          f"sections={len(rubric.sections)}")

    if dry_run:
        print("dry-run: rubric parsed and validated; nothing written")
        return 0

    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn:
        print("DATABASE_URL not set", file=sys.stderr)
        return 2

    import asyncpg

    conn = await asyncpg.connect(dsn, timeout=10)
    try:
        outcome = await ship_rubric(conn, team_id, rubric)
    finally:
        await conn.close()
    print(f"{outcome.table}: {outcome.version} {outcome.action}")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--team", required=True)
    ap.add_argument("--file", help="Ship a staged rubric JSON file instead of the team config's rubric")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    raise SystemExit(asyncio.run(_run(args.team, args.dry_run, args.file)))


if __name__ == "__main__":
    main()

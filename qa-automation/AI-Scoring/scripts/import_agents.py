#!/usr/bin/env python3
"""One-time (re-runnable) Mails-tab → qa.agents importer.

CutoverDesign.md §2: post-flip, agent identity comes from qa.agents, never
the Mails tab at runtime. This script is the sanctioned, operator-run
exception that seeds/refreshes qa.agents FROM the sheet — the only remaining
Sheets→Postgres flow, explicit and on-demand.

Mails tab layout: A=Name, B=Email, C=Supervisor, D=Canonical Name.

Usage:
    cd qa-automation/AI-Scoring
    python scripts/import_agents.py --team member_support [--dry-run]

Upserts on the uq_agents_team_lower_name index — re-running refreshes
emails/supervisors without duplicating rows. Rows without an email are
skipped (qa.agents.email is NOT NULL).

Departures: an agent absent from the Mails tab is marked ``active=FALSE``
— never deleted; their evals keep the identity link and the dashboards
degrade them to the departed treatment. (Temporary flow: qa.agents CRUD
moves to the dashboard frontend later; until then the Mails tab remains
the roster source and this script is the sync.) As a safety rail, a
non-dry run REFUSES to proceed when the Mails read comes back empty —
an empty read would otherwise deactivate the whole team.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

_AI_SCORING = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_AI_SCORING))
load_dotenv(_AI_SCORING / ".env")

from backend.config.team_config import get_team_config  # noqa: E402
from backend.services.sheets_service import _get_sheet  # noqa: E402

_UPSERT = """
INSERT INTO qa.agents (team_id, name, canonical_name, email, supervisor_email)
VALUES ($1, $2, $3, $4, $5)
ON CONFLICT (team_id, LOWER(name)) DO UPDATE SET
    canonical_name   = EXCLUDED.canonical_name,
    email            = EXCLUDED.email,
    supervisor_email = EXCLUDED.supervisor_email,
    active           = TRUE,
    updated_at       = NOW()
"""

# Departure sync: active rows whose name no longer appears in the Mails tab.
# Soft-deactivate only — deleting would orphan evals' agent_id links.
_SELECT_DEPARTED = """
SELECT name FROM qa.agents
WHERE team_id = $1 AND active AND LOWER(name) != ALL($2::text[])
ORDER BY name
"""

_DEACTIVATE = """
UPDATE qa.agents SET active = FALSE, updated_at = NOW()
WHERE team_id = $1 AND active AND LOWER(name) != ALL($2::text[])
RETURNING name
"""


def _read_mails_rows(team_id: str) -> list[tuple[str, str | None, str, str | None]]:
    config = get_team_config(team_id)
    sheet = _get_sheet(config, config.sheets.mails_tab)
    rows = sheet.get_all_values()
    out, skipped = [], 0
    for row in rows[1:]:  # skip header
        row = (row + [""] * 4)[:4]
        name, email, supervisor, canonical = (c.strip() for c in row)
        if not name:
            continue
        if not email:
            skipped += 1
            continue
        out.append((name, canonical or None, email, supervisor or None))
    if skipped:
        print(f"skipped {skipped} row(s) without an email (qa.agents.email is NOT NULL)")
    return out


async def _run(team_id: str, dry_run: bool) -> int:
    agents = _read_mails_rows(team_id)
    print(f"team={team_id}: {len(agents)} agent row(s) from the Mails tab")
    lower_names = [name.lower() for name, _, _, _ in agents]

    dsn = os.environ.get("DATABASE_URL", "")

    if dry_run:
        for name, canonical, email, supervisor in agents[:10]:
            print(f"  {name} <{email}>" + (f" (canonical: {canonical})" if canonical else ""))
        if dsn and agents:
            import asyncpg
            conn = await asyncpg.connect(dsn, timeout=30)
            try:
                departed = await conn.fetch(_SELECT_DEPARTED, team_id, lower_names)
            finally:
                await conn.close()
            if departed:
                print(f"would deactivate {len(departed)} departed agent(s): "
                      f"{[r['name'] for r in departed]}")
        print("dry-run: nothing written")
        return 0

    if not dsn:
        print("DATABASE_URL not set", file=sys.stderr)
        return 2
    if not agents:
        # An empty Mails read + the departure sync below would deactivate
        # the entire team. Whatever caused it (wrong tab, API hiccup),
        # nothing good comes from proceeding.
        print("Mails tab read came back EMPTY — refusing to sync "
              "(would deactivate every agent)", file=sys.stderr)
        return 2

    import asyncpg

    conn = await asyncpg.connect(dsn, timeout=30)
    try:
        async with conn.transaction():
            for name, canonical, email, supervisor in agents:
                await conn.execute(_UPSERT, team_id, name, canonical, email, supervisor)
            departed = await conn.fetch(_DEACTIVATE, team_id, lower_names)
        total = await conn.fetchval(
            "SELECT COUNT(*) FROM qa.agents WHERE team_id = $1 AND active", team_id
        )
    finally:
        await conn.close()
    if departed:
        print(f"deactivated {len(departed)} departed agent(s): "
              f"{[r['name'] for r in departed]}")
    print(f"qa.agents upserted; {total} active agent(s) for {team_id}")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    # --team-id accepted as an alias: the B-series / repair scripts use it,
    # and the mismatch has already eaten one operator run.
    ap.add_argument("--team", "--team-id", dest="team", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    raise SystemExit(asyncio.run(_run(args.team, args.dry_run)))


if __name__ == "__main__":
    main()

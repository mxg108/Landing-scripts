#!/usr/bin/env python3
"""Shadow sync: Railway Postgres → Sandy D1 (interim pull-based double-write).

The DESIGNED Phase-5 path is Railway pushing finalized evaluations to Sandy
with an App Service Token — blocked on Engineering ask #3. Until then this
script IS the shadow write: it re-syncs the qa_* tables (full fidelity —
row UPDATEs included, which an id-watermark would miss) and publishes
`eval_approved` rows into qa_events for evaluations that newly reached
`finalized` since the last sync, so the floor-TV SSE toasts fire from Sandy.

Runs from qa-automation/AI-Scoring (needs .env + .venv), on a cron:
    .venv/bin/python ../../sandy-qa/scripts/shadow_sync.py
"""

import asyncio
import importlib.util
import json
import pathlib
import subprocess
import sys
from datetime import datetime, timezone

_HERE = pathlib.Path(__file__).parent
spec = importlib.util.spec_from_file_location("pg_to_d1", _HERE / "pg_to_d1.py")
pg_to_d1 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pg_to_d1)

APP_ID = pg_to_d1.APP_ID
TABLES = pg_to_d1.TABLES

# qa_* wipe order: children before parents (D1 enforces FKs), agents last.
WIPE_ORDER = [
    "qa_agent_stat_points", "qa_coaching_evaluations", "qa_coachings",
    "qa_evaluation_tags", "qa_assessment_sections", "qa_assessments",
    "qa_formula_compliance_sweeps", "qa_evaluation_sections",
    "qa_evaluations", "qa_agents",
    # FK-free tables re-imported by RESYNC — wipe or their PKs collide.
    "qa_tags", "qa_score_audit", "qa_score_audit_archive", "qa_api_audit_log",
]
# Re-import: agents first, then everything from qa_evaluations onward in
# pg_to_d1's FK-safe order (cc_* tables stay on their initial snapshot until
# the CC slice; dispositions freshness is not shadow-gating).
RESYNC = [("qa.agents", "qa_agents")] + [
    t for t in TABLES if t[1] in (
        "qa_evaluations", "qa_evaluation_sections", "qa_formula_compliance_sweeps",
        "qa_agent_stat_points", "qa_tags", "qa_evaluation_tags", "qa_coachings",
        "qa_coaching_evaluations", "qa_assessments", "qa_assessment_sections",
        "qa_score_audit", "qa_score_audit_archive", "qa_api_audit_log",
    )
]


def eval_id_from_link(link: str) -> str:
    if not link:
        return ""
    clean = link.split("[")[0].strip().split("?")[0].strip()
    return clean.rstrip("/").split("/")[-1]


def d1_finalized_ids() -> set[int]:
    r = subprocess.run(
        [sys.executable, pg_to_d1.SANDY, "db", "query", APP_ID,
         "SELECT id FROM qa_evaluations WHERE state='finalized'"],
        capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"d1 id snapshot failed: {(r.stdout + r.stderr)[:300]}")
    return {row["id"] for row in json.loads(r.stdout)["data"][0]["results"]}


def truncate(s, n=280):
    s = s or ""
    return s if len(s) <= n else s[: n - 1] + "…"


async def main():
    import asyncpg
    started = datetime.now(timezone.utc)
    before_ids = d1_finalized_ids()

    conn = await asyncpg.connect(pg_to_d1.env_value("DATABASE_URL", pathlib.Path(".env")), timeout=30)
    try:
        # 1. wipe + full re-import of the qa_* tree (row updates included)
        pg_to_d1.apply_sql(
            "PRAGMA defer_foreign_keys = true;\n"
            + "\n".join(f"DELETE FROM {t};" for t in WIPE_ORDER)
        )
        for pg_name, d1_name in RESYNC:
            await pg_to_d1.import_table(conn, pg_name, d1_name, wipe=False)

        # 2. publish eval_approved for evals that newly reached finalized
        after_ids = d1_finalized_ids()
        new_ids = sorted(after_ids - before_ids)
        published = 0
        if new_ids:
            rows = await conn.fetch(
                "SELECT id, team_id, agent_name_raw, evaluator_email, overall_score, "
                "call_summary, key_strengths, opportunities, dialpad_link, "
                "dialpad_call_id, approved_at "
                "FROM qa.evaluations WHERE id = ANY($1::bigint[])", new_ids)
            stmts = []
            for r in rows:
                payload = {
                    "call_id": r["dialpad_call_id"] or "",
                    "eval_id": eval_id_from_link(r["dialpad_link"] or "") or (r["dialpad_call_id"] or ""),
                    "history_row": None,
                    "agent": r["agent_name_raw"] or "",
                    "evaluator_email": r["evaluator_email"] or "",
                    "overall_score": float(r["overall_score"]) if r["overall_score"] is not None else None,
                    "summary": truncate(r["call_summary"]),
                    "strengths": truncate(r["key_strengths"]),
                    "opportunities": truncate(r["opportunities"]),
                    "dialpad_link": r["dialpad_link"] or "",
                    "timestamp": (r["approved_at"] or started).isoformat(),
                }
                stmts.append(
                    "INSERT INTO qa_events (team_id, type, payload) VALUES ("
                    + pg_to_d1.lit(r["team_id"]) + ", 'eval_approved', "
                    + pg_to_d1.lit(json.dumps(payload)) + ");")
            pg_to_d1.apply_sql("\n".join(stmts))
            published = len(stmts)

        # 3. spot reconciliation on the two load-bearing tables
        ok = True
        for pg_name, d1_name in (("qa.evaluations", "qa_evaluations"),
                                 ("qa.evaluation_sections", "qa_evaluation_sections")):
            pg_row = await conn.fetchrow(
                f"SELECT count(*) c, COALESCE(sum(id),0)::bigint s FROM {pg_name}")
            d1_row = pg_to_d1.d1_query(
                f"SELECT count(*) c, COALESCE(sum(id),0) s FROM {d1_name}")[0]
            ok &= (pg_row["c"], pg_row["s"]) == (d1_row["c"], d1_row["s"])
        secs = (datetime.now(timezone.utc) - started).total_seconds()
        print(f"SHADOW SYNC {'OK' if ok else 'RECONCILE MISMATCH'}: "
              f"{len(after_ids)} finalized evals, +{published} events published, {secs:.0f}s")
        sys.exit(0 if ok else 2)
    finally:
        await conn.close()


asyncio.run(main())

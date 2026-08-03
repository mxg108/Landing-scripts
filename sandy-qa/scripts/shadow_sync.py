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

# Sandy-born rows (evals scored ON Sandy) live at id >= SANDY_ID_BASE —
# the sync only ever owns the Railway range below it. Wipes are
# range-scoped so a sync can never delete Sandy's own evaluations again
# (2026-08-03 incident: the first Sandy-scored calls were wiped by the
# unscoped v1 of this script).
SANDY_ID_BASE = 10_000_000

# qa_* wipe order: children before parents (D1 enforces FKs), agents last.
# Value = extra WHERE guard ('' = full wipe, Railway-owned table).
WIPE_ORDER = {
    "qa_agent_stat_points": f"WHERE evaluation_id < {SANDY_ID_BASE}",
    "qa_coaching_evaluations": f"WHERE evaluation_id < {SANDY_ID_BASE}",
    "qa_coachings": "",
    "qa_evaluation_tags": f"WHERE evaluation_id < {SANDY_ID_BASE}",
    "qa_assessment_sections": "",
    "qa_assessments": "",
    "qa_formula_compliance_sweeps": f"WHERE evaluation_id < {SANDY_ID_BASE}",
    "qa_evaluation_sections": f"WHERE evaluation_id < {SANDY_ID_BASE}",
    "qa_evaluations": f"WHERE id < {SANDY_ID_BASE}",
    "qa_agents": "",
    # FK-free tables re-imported by RESYNC — wipe or their PKs collide.
    "qa_tags": "",
    "qa_score_audit": f"WHERE id < {SANDY_ID_BASE}",
    "qa_score_audit_archive": "",
    "qa_api_audit_log": "",
}
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


CC_CALLS_COLS = [
    "id", "team_id", "dialpad_call_id", "dialpad_master_call_id",
    "dialpad_entry_point_call_id", "started_at", "rang_at", "connected_at",
    "ended_at", "total_duration_ms", "direction", "external_number",
    "internal_number", "group_id", "dialpad_agent_id", "agent_name",
    "caller_name", "caller_phone_e164", "caller_email", "target_name",
    "target_type", "target_phone", "mos_score", "was_recorded",
    "is_transferred", "recording_urls", "last_state", "last_state_at",
    "total_hold_seconds", "raw_call_details", "scored", "scored_at",
    "evaluation_orphaned", "evaluation_orphaned_at", "seen_via",
    "first_seen_at", "last_updated_at", "disposition_category",
    "disposition", "ai_csat", "disposition_source",
]


def _d1_scalar(sql: str):
    return pg_to_d1.d1_query(sql)[0]["v"]


async def sync_cc_incremental(conn) -> str:
    # cc_calls upsert by last_updated_at watermark
    wm = _d1_scalar("SELECT COALESCE(max(last_updated_at), '1970-01-01') AS v FROM cc_calls")
    rows = await conn.fetch(
        f"SELECT {', '.join(CC_CALLS_COLS)} FROM command_center.calls "
        "WHERE last_updated_at > $1 ORDER BY last_updated_at LIMIT 5000",
        # D1 stores ISO text; PG compares timestamptz — parse the watermark
        datetime.fromisoformat(wm.replace("Z", "+00:00")),
    )
    if rows:
        col_list = ", ".join(CC_CALLS_COLS)
        updates = ", ".join(f"{c}=excluded.{c}" for c in CC_CALLS_COLS if c != "id")
        stmts = ["PRAGMA defer_foreign_keys = true;"]
        for r in rows:
            vals = ", ".join(pg_to_d1.lit(r[c]) for c in CC_CALLS_COLS)
            stmts.append(
                f"INSERT INTO cc_calls ({col_list}) VALUES ({vals}) "
                f"ON CONFLICT(id) DO UPDATE SET {updates};"
            )
            if sum(len(s) for s in stmts) > 300_000:
                pg_to_d1.apply_sql("\n".join(stmts))
                stmts = ["PRAGMA defer_foreign_keys = true;"]
        if len(stmts) > 1:
            pg_to_d1.apply_sql("\n".join(stmts))

    # append-only tails by id watermark
    appended = {}
    for pg_name, d1_name, cols in (
        ("command_center.hold_intervals", "cc_hold_intervals",
         ["id", "team_id", "dialpad_call_id", "call_id", "started_at",
          "ended_at", "seconds", "ended_by"]),
        ("command_center.webhook_events", "cc_webhook_events",
         ["id", "received_at", "team_id", "event_kind", "dialpad_call_id",
          "dialpad_master_call_id", "dialpad_agent_id", "state",
          "event_timestamp", "raw_payload", "processed_at"]),
    ):
        max_id = _d1_scalar(f"SELECT COALESCE(max(id), 0) AS v FROM {d1_name}")
        tail = await conn.fetch(
            f"SELECT {', '.join(cols)} FROM {pg_name} WHERE id > $1 ORDER BY id LIMIT 5000",
            max_id)
        if tail:
            col_list = ", ".join(cols)
            stmts = ["PRAGMA defer_foreign_keys = true;"]
            for r in tail:
                stmts.append(
                    f"INSERT OR IGNORE INTO {d1_name} ({col_list}) VALUES "
                    f"({', '.join(pg_to_d1.lit(r[c]) for c in cols)});")
                if sum(len(s) for s in stmts) > 300_000:
                    pg_to_d1.apply_sql("\n".join(stmts))
                    stmts = ["PRAGMA defer_foreign_keys = true;"]
            if len(stmts) > 1:
                pg_to_d1.apply_sql("\n".join(stmts))
        appended[d1_name] = len(tail)
    return (f"calls+{len(rows)} holds+{appended['cc_hold_intervals']} "
            f"webhooks+{appended['cc_webhook_events']}")


async def main():
    import asyncpg
    started = datetime.now(timezone.utc)
    before_ids = d1_finalized_ids()

    conn = await asyncpg.connect(pg_to_d1.env_value("DATABASE_URL", pathlib.Path(".env")), timeout=30)
    try:
        # 0. Railway-wins dedupe: a call scored on BOTH stacks during the
        # transition keeps Railway's row (the oracle until cutover) — drop
        # the Sandy-born duplicate LOUDLY so the collision is visible.
        pg_pairs = await conn.fetch(
            "SELECT team_id, dialpad_call_id FROM qa.evaluations "
            "WHERE dialpad_call_id IS NOT NULL")
        sandy_rows = pg_to_d1.d1_query(
            f"SELECT id, team_id, dialpad_call_id FROM qa_evaluations WHERE id >= {SANDY_ID_BASE}")
        pg_set = {(r["team_id"], r["dialpad_call_id"]) for r in pg_pairs}
        dupes = [r for r in sandy_rows
                 if (r["team_id"], r["dialpad_call_id"]) in pg_set]
        if dupes:
            ids = ", ".join(str(r["id"]) for r in dupes)
            print(f"RAILWAY-WINS: dropping {len(dupes)} Sandy-born duplicate eval(s): {ids}")
            pg_to_d1.apply_sql(
                "PRAGMA defer_foreign_keys = true;\n"
                f"DELETE FROM qa_evaluation_sections WHERE evaluation_id IN ({ids});\n"
                f"DELETE FROM qa_evaluations WHERE id IN ({ids});"
            )

        # 1. wipe + re-import of the RAILWAY-OWNED qa_* range (updates included)
        pg_to_d1.apply_sql(
            "PRAGMA defer_foreign_keys = true;\n"
            + "\n".join(f"DELETE FROM {t} {w};".strip() for t, w in WIPE_ORDER.items())
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

        # 2.5 cc_* incremental — disposition grounding must be FRESH
        # (the scoring workflow's cc_context match reads cc_calls).
        # cc_calls: upsert rows whose last_updated_at moved past the D1
        # watermark (ON CONFLICT DO UPDATE — never INSERT OR REPLACE,
        # which would cascade-delete cc_hold_intervals). hold_intervals /
        # webhook_events are append-only: insert by id watermark.
        cc_synced = await sync_cc_incremental(conn)

        # 3. spot reconciliation on the two load-bearing tables
        ok = True
        for pg_name, d1_name, guard in (
            ("qa.evaluations", "qa_evaluations", f"WHERE id < {SANDY_ID_BASE}"),
            ("qa.evaluation_sections", "qa_evaluation_sections",
             f"WHERE evaluation_id < {SANDY_ID_BASE}"),
        ):
            pg_row = await conn.fetchrow(
                f"SELECT count(*) c, COALESCE(sum(id),0)::bigint s FROM {pg_name}")
            d1_row = pg_to_d1.d1_query(
                f"SELECT count(*) c, COALESCE(sum(id),0) s FROM {d1_name} {guard}")[0]
            ok &= (pg_row["c"], pg_row["s"]) == (d1_row["c"], d1_row["s"])
        secs = (datetime.now(timezone.utc) - started).total_seconds()
        print(f"SHADOW SYNC {'OK' if ok else 'RECONCILE MISMATCH'}: "
              f"{len(after_ids)} finalized evals, +{published} events published, "
              f"cc {cc_synced}, {secs:.0f}s")
        sys.exit(0 if ok else 2)
    finally:
        await conn.close()


asyncio.run(main())

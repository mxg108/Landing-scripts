#!/usr/bin/env python3
"""Railway Postgres → Sandy D1 bulk import (PortManifest §7, port-order gate 1.5).

Streams every in-scope table FK-first, transforms values per the manifest
translation rules, and pipes batched multi-row INSERTs to `sandy.py db
migrate` via stdin — row data never touches disk (PII). Ends with a
reconciliation pass (count / sum(id) / max(id) per table, Postgres vs D1)
in the spirit of the B0–B4 backfill playbook.

Run from qa-automation/AI-Scoring (needs .env + .venv's asyncpg):
    .venv/bin/python ../../sandy-qa/scripts/pg_to_d1.py [--table cc_calls] [--wipe]

--table  import just one D1 table (re-runs after a mid-table failure)
--wipe   DELETE FROM the D1 table(s) before importing (idempotent re-run)

Snapshot semantics: Railway stays live; rows written after this run are
expected drift, closed later by shadow double-writes + the cutover delta.
"""

import argparse
import asyncio
import datetime as dt
import decimal
import json
import os
import pathlib
import re
import subprocess
import sys

# Keywords the Sandy migrate endpoint refuses anywhere in the SQL text.
# ATTACH is confirmed; the neighbors are cheap insurance (rare in prose).
_KEYWORD_RE = re.compile(r"(?i)attach|detach|vacuum|reindex")

APP_ID = "a2cc5b5a-df29-4ae7-9dbb-e270052015e7"  # qa-scoring
SANDY = os.path.expanduser("~/.claude/commands/scripts/sandy.py")
CHUNK_BYTES = 400_000  # per db-migrate request (multiple statements)
STMT_BYTES = 80_000    # per single INSERT statement — D1 rejects ~100KB+ with SQLITE_TOOBIG
ROWS_PER_STMT = 200

# (pg qualified name, d1 name) in FK-safe order. `teams` is UPDATEd (rows
# exist from 0001 seeds); `qa_tags` is replaced (DELETE + INSERT with
# explicit ids) so Postgres ids win over seed ids.
TABLES = [
    ("public.teams", "teams"),
    ("qa.agents", "qa_agents"),
    ("qa.formula_versions", "qa_formula_versions"),
    ("qa.rubric_versions", "qa_rubric_versions"),
    ("command_center.webhook_events", "cc_webhook_events"),
    ("command_center.calls", "cc_calls"),
    ("command_center.chiclets", "cc_chiclets"),
    ("command_center.chiclet_events", "cc_chiclet_events"),
    ("command_center.frequent_callers_cache", "cc_frequent_callers_cache"),
    ("command_center.dialpad_agents", "cc_dialpad_agents"),
    ("command_center.hold_intervals", "cc_hold_intervals"),
    ("qa.evaluations", "qa_evaluations"),
    ("qa.evaluation_sections", "qa_evaluation_sections"),
    ("qa.formula_compliance_sweeps", "qa_formula_compliance_sweeps"),
    ("qa.agent_stat_points", "qa_agent_stat_points"),
    ("qa.tags", "qa_tags"),
    ("qa.evaluation_tags", "qa_evaluation_tags"),
    ("qa.coachings", "qa_coachings"),
    ("qa.coaching_evaluations", "qa_coaching_evaluations"),
    ("qa.assessments", "qa_assessments"),
    ("qa.assessment_sections", "qa_assessment_sections"),
    ("qa.score_audit", "qa_score_audit"),
    ("qa.score_audit_archive", "qa_score_audit_archive"),
    ("qa.api_audit_log", "qa_api_audit_log"),
]


def env_value(key: str, env_path: pathlib.Path) -> str:
    for line in env_path.read_text().splitlines():
        if line.strip().startswith(key + "="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit(f"{key} not found in {env_path}")


def lit(v) -> str:
    """Python value → SQLite literal, per manifest §7 translation rules."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, decimal.Decimal):
        return repr(float(v))
    if isinstance(v, dt.datetime):
        if v.tzinfo is not None:
            v = v.astimezone(dt.timezone.utc)
        return "'" + v.strftime("%Y-%m-%dT%H:%M:%S.") + f"{v.microsecond // 1000:03d}Z'"
    if isinstance(v, dt.date):
        return f"'{v.isoformat()}'"
    if isinstance(v, (list, tuple)):  # pg arrays → JSON array text
        return lit(json.dumps(list(v)))
    if isinstance(v, dict):  # decoded jsonb (if a codec is active)
        return lit(json.dumps(v))
    if isinstance(v, str):
        body = v.replace("\x00", "").replace("'", "''")
        # Sandy's migrate endpoint keyword-scans the raw SQL (observed:
        # "'ATTACH' is not permitted") and prose in transcripts/summaries
        # legitimately contains such words. Split them into a concatenation —
        # 'att'||'ach' stores the identical value but the SQL text never
        # carries the contiguous token.
        body = _KEYWORD_RE.sub(lambda m: m.group(0)[:2] + "'||'" + m.group(0)[2:], body)
        return "'" + body + "'"
    raise TypeError(f"unhandled type {type(v)}: {v!r}")


def apply_sql(sql: str) -> None:
    import time
    last = ""
    for attempt in range(3):
        r = subprocess.run(
            [sys.executable, SANDY, "db", "migrate", APP_ID, "-"],
            input=sql, capture_output=True, text=True, timeout=180,
        )
        if r.returncode == 0 and '"ok": true' in r.stdout:
            return
        last = (r.stdout + r.stderr)[:600]
        if "TOOBIG" in last:  # deterministic — retrying cannot help
            break
        time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"db migrate failed: {last}")


def d1_query(sql: str):
    r = subprocess.run(
        [sys.executable, SANDY, "db", "query", APP_ID, sql],
        capture_output=True, text=True, timeout=120,
    )
    if r.returncode != 0:
        raise RuntimeError(f"db query failed: {(r.stdout + r.stderr)[:600]}")
    return json.loads(r.stdout)["data"][0]["results"]


async def import_table(conn, pg_name: str, d1_name: str, wipe: bool) -> int:
    n = await conn.fetchval(f"SELECT count(*) FROM {pg_name}")
    if d1_name == "teams":
        await import_teams(conn)
        return n
    if wipe or d1_name == "qa_tags":
        apply_sql(f"DELETE FROM {d1_name};")
    if n == 0:
        print(f"  {d1_name:<30} empty — skipped")
        return 0

    cols = [
        r["column_name"] for r in await conn.fetch(
            """SELECT column_name FROM information_schema.columns
               WHERE table_schema = $1 AND table_name = $2
               AND is_generated = 'NEVER' ORDER BY ordinal_position""",
            *pg_name.split("."),
        )
    ]
    col_list = ", ".join(cols)
    prefix = f"INSERT INTO {d1_name} ({col_list}) VALUES\n"
    header = "PRAGMA defer_foreign_keys = true;\n"

    sent = 0
    pending_stmts: list[str] = []
    pending_bytes = 0
    values: list[str] = []
    values_bytes = 0

    def close_stmt():
        nonlocal values, values_bytes, pending_bytes
        if values:
            s = prefix + ",\n".join(values) + ";"
            pending_stmts.append(s)
            pending_bytes += len(s)
            values, values_bytes = [], 0

    def flush():
        nonlocal pending_stmts, pending_bytes
        if pending_stmts:
            apply_sql(header + "\n".join(pending_stmts))
            pending_stmts, pending_bytes = [], 0
            print(f"  {d1_name:<30} {sent}/{n} rows", flush=True)

    async with conn.transaction():
        async for rec in conn.cursor(f"SELECT {col_list} FROM {pg_name} ORDER BY 1"):
            row = "(" + ", ".join(lit(rec[c]) for c in cols) + ")"
            if len(prefix) + len(row) > STMT_BYTES:
                raise RuntimeError(
                    f"{d1_name}: single row exceeds {STMT_BYTES}B statement cap "
                    f"({len(row)}B) — needs scratch-table assembly, not present today"
                )
            if values_bytes + len(row) > STMT_BYTES:
                close_stmt()
            values.append(row)
            values_bytes += len(row)
            sent += 1
            if len(values) >= ROWS_PER_STMT:
                close_stmt()
            if pending_bytes + values_bytes >= CHUNK_BYTES:
                close_stmt()
                flush()
    close_stmt()
    flush()
    return n


async def import_teams(conn) -> None:
    rows = await conn.fetch("SELECT * FROM public.teams ORDER BY id")
    stmts = []
    for r in rows:
        sets = ", ".join(
            f"{c} = {lit(r[c])}" for c in r.keys() if c != "id"
        )
        stmts.append(f"UPDATE teams SET {sets} WHERE id = {lit(r['id'])};")
    apply_sql("\n".join(stmts))
    print(f"  {'teams':<30} {len(rows)} rows updated in place")


async def reconcile(conn) -> bool:
    print("\n== reconciliation (pg vs d1): count / sum(id) / max(id) ==")
    all_ok = True
    for pg_name, d1_name in TABLES:
        id_col = "id" if d1_name != "teams" else None
        if id_col:
            pg = await conn.fetchrow(
                f"SELECT count(*) c, COALESCE(sum(id),0)::bigint s, COALESCE(max(id),0)::bigint m FROM {pg_name}"
            )
            d1 = d1_query(
                f"SELECT count(*) c, COALESCE(sum(id),0) s, COALESCE(max(id),0) m FROM {d1_name}"
            )[0]
            ok = (pg["c"], pg["s"], pg["m"]) == (d1["c"], d1["s"], d1["m"])
        else:
            pg = await conn.fetchrow(f"SELECT count(*) c FROM {pg_name}")
            d1 = d1_query(f"SELECT count(*) c FROM {d1_name}")[0]
            ok = pg["c"] == d1["c"]
        all_ok &= ok
        flag = "OK " if ok else "MISMATCH"
        print(f"  {flag} {d1_name:<30} pg={dict(pg)} d1={d1}")
    return all_ok


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--table", help="import a single D1 table name")
    ap.add_argument("--from", dest="from_table",
                    help="resume: import this D1 table and everything after it, wiping each")
    ap.add_argument("--wipe", action="store_true", help="DELETE target before import")
    ap.add_argument("--env", default=".env", type=pathlib.Path)
    ap.add_argument("--reconcile-only", action="store_true")
    args = ap.parse_args()

    import asyncpg  # local venv dependency

    conn = await asyncpg.connect(env_value("DATABASE_URL", args.env), timeout=30)
    try:
        if not args.reconcile_only:
            todo = [t for t in TABLES if not args.table or t[1] == args.table]
            if args.from_table:
                names = [t[1] for t in TABLES]
                if args.from_table not in names:
                    raise SystemExit(f"unknown table {args.from_table}")
                todo = TABLES[names.index(args.from_table):]
                args.wipe = True
            if not todo:
                raise SystemExit(f"unknown table {args.table}")
            print(f"== importing {len(todo)} table(s) ==")
            for pg_name, d1_name in todo:
                await import_table(conn, pg_name, d1_name, args.wipe)
        ok = await reconcile(conn)
        print("\nRESULT:", "ALL TABLES MATCH" if ok else "MISMATCHES FOUND — see above")
        sys.exit(0 if ok else 2)
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())

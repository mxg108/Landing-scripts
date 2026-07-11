#!/usr/bin/env python3
"""B1 — import the B0 staging file into qa.evaluations (BackfillPlan §5).

One off-peak batch, no external calls. Ships the team's archived
``v0_sheet`` formula row first (version_ship.py defers historic archives
to B1 by design — the evaluations FK needs it), then upserts staged
evaluations by natural key: already-imported rows are skipped, so
re-runs after a partial failure resume instead of duplicating.

Overnight contract: every row failure is caught, logged, and reported —
the run never dies mid-batch. §7.4 correctness checks run at the end
(counts + overall-score sum, DB vs staging). Exit 0 = clean; 2 = ran
with skips/failures or check drift (read the report); 1 = structural
(preflight failed, nothing written).

Rows B0 marked ``import_blocked`` (no usable clock — they would violate
the finalized CHECKs) are excluded here and wait for B2's Dialpad
repair; they are counted in the report, not silently dropped.

Usage:
    cd qa-automation/AI-Scoring
    python3 scripts/backfill_seed_b1.py --team-id member_support --dry-run
    python3 scripts/backfill_seed_b1.py --team-id member_support
    python3 scripts/backfill_seed_b1.py --team-id sales --limit 20   # smoke
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

_AI_SCORING = Path(__file__).resolve().parent.parent
_REPO_ROOT = _AI_SCORING.parent.parent
_STAGING_DIR = _REPO_ROOT / "database" / "backfill_staging"
load_dotenv(_AI_SCORING / ".env")

import os  # noqa: E402

V0_SHEET_JSON = {
    "member_support": _AI_SCORING / "backend" / "config" / "scoring" / "member_support" / "v0_sheet.json",
    "sales": _AI_SCORING / "backend" / "config" / "scoring" / "sales" / "v0_sheet.json",
}
FORMULA_VERSION = {
    "member_support": "member_support_v0_sheet",
    "sales": "sales_v0_sheet",
}

# staged (kind, era) -> qa.evaluation_sections.score_type
SCORE_TYPE = {
    ("numeric", "ai"): "numeric",
    ("numeric", "manual"): "manual_numeric",
    ("yn", "ai"): "binary",
    ("yn", "manual"): "manual_binary",
    ("manual", "ai"): "manual_numeric",
    ("manual", "manual"): "manual_numeric",
    ("manual_yn", "ai"): "manual_binary",
    ("manual_yn", "manual"): "manual_binary",
}

_EVAL_INSERT = """
INSERT INTO qa.evaluations (
    team_id, agent_name_raw, agent_email, evaluator_email, state, source,
    call_connected_at, dialpad_link, overall_score,
    formula_version, rubric_version, models_used, ai_provider_primary,
    key_strengths, opportunities, call_summary, caller_name, caller_phone,
    dialpad_call_metadata, scoring_status, approved_at, finalized_at
) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12::jsonb,$13,$14,$15,$16,$17,$18,
          $19::jsonb,'complete',$20,$21)
RETURNING id
"""

_SECTION_INSERT = """
INSERT INTO qa.evaluation_sections (
    evaluation_id, section_id, section_number, score_type,
    numeric_score, binary_value, score_source, ai_provider, model,
    confidence, reasoning
) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
"""

_NK_PATH_SQL = "dialpad_call_metadata #>> '{backfill,natural_key}'"


def _ts(iso: str | None) -> datetime | None:
    return datetime.fromisoformat(iso) if iso else None


def _section_params(evaluation_id: int, staged_eval: dict) -> list[tuple]:
    params = []
    for number, sec in enumerate(staged_eval["sections"], start=1):
        value = sec["value"]
        numeric = value.get("numeric")
        binary = value.get("binary") or ("NA" if value.get("na") else None)
        is_ai = sec["score_source"] == "ai"
        params.append((
            evaluation_id, sec["section_id"], number,
            SCORE_TYPE[(sec["kind"], sec["era"])],
            numeric, binary, sec["score_source"],
            "gemini" if is_ai else None,
            "gemini-2.5-flash" if is_ai else None,
            sec["confidence"], sec["reasoning"],
        ))
    return params


async def ship_v0_sheet_formula(conn, team_id: str, staged: list[dict], dry_run: bool) -> str:
    """Insert the archived legacy formula row if absent (FK prerequisite)."""
    version = FORMULA_VERSION[team_id]
    exists = await conn.fetchval(
        "SELECT 1 FROM qa.formula_versions WHERE formula_version = $1", version)
    if exists:
        return "already_present"
    payload = json.loads(V0_SHEET_JSON[team_id].read_text(encoding="utf-8"))
    declared = payload.get("formula_id")
    if declared != version:
        raise SystemExit(
            f"structural: {V0_SHEET_JSON[team_id]} declares formula_id="
            f"{declared!r}, expected {version!r}")
    clocks = sorted(s["approved_at"] for s in staged if s["approved_at"])
    if dry_run:
        return "would_insert"
    await conn.execute(
        "INSERT INTO qa.formula_versions "
        "(formula_version, team_id, formula_json, effective_from, effective_until) "
        "VALUES ($1,$2,$3::jsonb,$4,$5) ON CONFLICT (formula_version) DO NOTHING",
        version, team_id, json.dumps(payload), _ts(clocks[0]), _ts(clocks[-1]),
    )
    return "inserted"


async def run(args) -> int:
    import asyncpg

    staging_path = Path(args.staging_dir) / f"staging_{args.team_id}.jsonl"
    if not staging_path.exists():
        print(f"structural: staging file missing: {staging_path} — run B0 first")
        return 1
    staged_all = [json.loads(line) for line in staging_path.open(encoding="utf-8")]
    blocked = [s for s in staged_all if s["import_blocked"]]
    importable = [s for s in staged_all if not s["import_blocked"]]
    if args.limit:
        importable = importable[: args.limit]

    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn:
        print("structural: DATABASE_URL not set")
        return 1
    conn = await asyncpg.connect(dsn, timeout=15)

    counts = {
        "staged_total": len(staged_all), "import_blocked": len(blocked),
        "candidates": len(importable), "already_imported": 0,
        "link_collisions": 0, "inserted": 0, "failed": 0,
    }
    failures: list[dict] = []

    try:
        # ---- preflight -----------------------------------------------------
        rubric = staged_all[0]["rubric_version"]
        if not await conn.fetchval(
                "SELECT 1 FROM qa.rubric_versions WHERE rubric_version = $1", rubric):
            print(f"structural: rubric_version {rubric!r} not in qa.rubric_versions")
            return 1
        formula_state = await ship_v0_sheet_formula(conn, args.team_id, staged_all, args.dry_run)

        existing_keys = {
            r[0] for r in await conn.fetch(
                f"SELECT {_NK_PATH_SQL} FROM qa.evaluations "
                f"WHERE team_id = $1 AND {_NK_PATH_SQL} IS NOT NULL", args.team_id)
        }
        to_import = []
        for s in importable:
            if s["natural_key"] in existing_keys:
                counts["already_imported"] += 1
            else:
                to_import.append(s)

        staged_links = [s["dialpad_link"] for s in to_import if s["dialpad_link"]]
        collisions = set()
        if staged_links:
            collisions = {
                r["dialpad_link"] for r in await conn.fetch(
                    "SELECT dialpad_link FROM qa.evaluations "
                    "WHERE team_id = $1 AND dialpad_link = ANY($2::text[])",
                    args.team_id, staged_links)
            }
        if collisions:
            counts["link_collisions"] = len(collisions)
            for s in [x for x in to_import if x["dialpad_link"] in collisions]:
                failures.append({"natural_key": s["natural_key"], "line": s["line"],
                                 "error": f"dialpad_link already on a live row: {s['dialpad_link']}"})
            to_import = [x for x in to_import if x["dialpad_link"] not in collisions]

        print(f"[B1:{args.team_id}] staged={counts['staged_total']} "
              f"blocked={counts['import_blocked']} candidates={counts['candidates']} "
              f"already_imported={counts['already_imported']} "
              f"link_collisions={counts['link_collisions']} "
              f"to_import={len(to_import)} formula_row={formula_state}"
              + (" [DRY RUN]" if args.dry_run else ""))
        if args.dry_run:
            return 0

        # ---- import loop: one transaction per evaluation --------------------
        imported_at = datetime.now(timezone.utc).isoformat()
        for i, s in enumerate(to_import, start=1):
            metadata = {"backfill": {
                **s["annotations"],
                "natural_key": s["natural_key"],
                "staged_line": s["line"],
                "imported_at": imported_at,
            }}
            era_ai = s["era"] == "ai"
            try:
                async with conn.transaction():
                    evaluation_id = await conn.fetchval(
                        _EVAL_INSERT,
                        s["team_id"], s["agent_name_raw"], s["agent_email"],
                        s["evaluator_email"], s["state"], s["source"],
                        _ts(s["call_connected_at"]), s["dialpad_link"],
                        s["overall_score"], s["formula_version"], s["rubric_version"],
                        json.dumps(s["models_used"]),
                        "gemini" if era_ai else None,
                        s["key_strengths"], s["opportunities"], s["call_summary"],
                        s["caller_name"], s["caller_phone"],
                        json.dumps(metadata),
                        _ts(s["approved_at"]), _ts(s["approved_at"]),
                    )
                    await conn.executemany(_SECTION_INSERT, _section_params(evaluation_id, s))
                counts["inserted"] += 1
            except Exception as e:  # noqa: BLE001 — overnight contract: log, continue
                counts["failed"] += 1
                failures.append({"natural_key": s["natural_key"], "line": s["line"],
                                 "error": f"{type(e).__name__}: {e}"})
            if i % 200 == 0:
                print(f"  {i}/{len(to_import)} imported "
                      f"({counts['failed']} failed so far)")

        # ---- §7.4 checks -----------------------------------------------------
        db = await conn.fetchrow(
            f"SELECT COUNT(*) AS n, COALESCE(SUM(overall_score), 0) AS score_sum, "
            f"COUNT(*) FILTER (WHERE call_connected_at IS NULL) AS null_clock "
            f"FROM qa.evaluations WHERE team_id = $1 AND {_NK_PATH_SQL} IS NOT NULL",
            args.team_id)
        section_count = await conn.fetchval(
            f"SELECT COUNT(*) FROM qa.evaluation_sections es "
            f"JOIN qa.evaluations e ON e.id = es.evaluation_id "
            f"WHERE e.team_id = $1 AND e.{_NK_PATH_SQL} IS NOT NULL", args.team_id)
        expected_rows = counts["already_imported"] + counts["inserted"]
        if args.limit:
            # Partial smoke run: DB may hold rows outside the truncated
            # candidate list, so equality checks don't apply.
            checks = {"skipped_partial_run": True}
        else:
            complete = counts["failed"] == 0 and counts["link_collisions"] == 0
            checks = {
                "db_rows_equal_imported": db["n"] == expected_rows,
                "db_sections_equal_rows_x_n":
                    section_count == expected_rows * len(staged_all[0]["sections"]),
                "overall_sum_matches_staging": complete and float(db["score_sum"])
                    == round(sum(s["overall_score"] for s in importable), 1),
            }
    finally:
        await conn.close()

    report = {
        "stage": "B1", "team_id": args.team_id, "counts": counts,
        "checks": checks, "failures": failures,
        "db": {"rows": db["n"], "sections": section_count,
               "overall_sum": float(db["score_sum"]),
               "null_call_clock": db["null_clock"]},
    }
    report_path = Path(args.staging_dir) / f"report_{args.team_id}_b1.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"[B1:{args.team_id}] inserted={counts['inserted']} "
          f"failed={counts['failed']} already_imported={counts['already_imported']}")
    print(f"  db rows={db['n']} sections={section_count} "
          f"null-clock (B2 queue)={db['null_clock']}")
    for name, ok in checks.items():
        print(f"  check {name}: {'OK' if ok else 'DRIFTED'}")
    print(f"  report: {report_path}")

    clean = (counts["failed"] == 0 and counts["link_collisions"] == 0
             and all(checks.values()))
    return 0 if clean else 2


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--team-id", required=True, choices=sorted(FORMULA_VERSION))
    ap.add_argument("--staging-dir", default=str(_STAGING_DIR))
    ap.add_argument("--dry-run", action="store_true",
                    help="Preflight + counts only; no writes at all")
    ap.add_argument("--limit", type=int, default=0,
                    help="Import at most N rows (smoke runs)")
    args = ap.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())

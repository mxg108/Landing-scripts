#!/usr/bin/env python3
"""B2 — nightly Dialpad enrichment of backfilled rows (BackfillPlan §5).

Two phases per run, both resumable and rate-limited (~4 req/s, under
Dialpad's documented 5 req/s cap):

  R — repair-import: B0 rows that B1 skipped as ``import_blocked`` (no
      usable clock) get their call clock from Dialpad and import here,
      already enriched. Rows Dialpad can't resolve stay out and are
      reported each run (they're few and the lookups are cheap).

  E — enrich: cursor over imported backfill rows without
      ``backfill.enriched_at`` — resolve the Dialpad call, then write
      authoritative clocks (overwrites the sheet clock; the pre-enrich
      value is kept in metadata), call ids, direction, recording_urls
      (§3.4.2 shape), mos_score, and fill caller name/phone where the
      sheet had none. Deterministic 404/empty responses mark
      ``backfill.enrich_failed`` so they never retry forever; transient
      failures stay unmarked and retry next night.

Overnight contract: per-row isolation (one UPDATE/insert each), progress
lines, JSON report with failure detail, graceful abort on sustained
rate-limiting (finish tomorrow), exit 0 clean / 2 review / 1 structural.

Usage:
    cd qa-automation/AI-Scoring
    python3 scripts/backfill_seed_b2.py --team-id member_support --dry-run
    python3 scripts/backfill_seed_b2.py --team-id member_support --batch-size 400

Nightly cron candidate once the first supervised runs are boring:
    python3 scripts/backfill_seed_b2.py --team-id member_support
    python3 scripts/backfill_seed_b2.py --team-id sales
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

_AI_SCORING = Path(__file__).resolve().parent.parent
_REPO_ROOT = _AI_SCORING.parent.parent
_STAGING_DIR = _REPO_ROOT / "database" / "backfill_staging"
load_dotenv(_AI_SCORING / ".env")

if str(_AI_SCORING) not in sys.path:
    sys.path.insert(0, str(_AI_SCORING))

from backend.services.dialpad_client import (  # noqa: E402
    DialpadRateLimited,
    _epoch_ms_to_utc_datetime,
    get_call_details,
)

# Reuse B1's insert machinery for phase R (same natural-key idempotency).
_b1_spec = importlib.util.spec_from_file_location(
    "backfill_seed_b1", Path(__file__).resolve().parent / "backfill_seed_b1.py")
b1 = importlib.util.module_from_spec(_b1_spec)
_b1_spec.loader.exec_module(b1)

RATE_LIMIT_SECONDS = 0.25   # ~4 req/s
RATE_LIMIT_BACKOFF = 65     # one long sit-out on 429, then one retry
_NK = "dialpad_call_metadata #>> '{backfill,natural_key}'"
_ENRICHED = "dialpad_call_metadata #>> '{backfill,enriched_at}'"
_FAILED = "dialpad_call_metadata #>> '{backfill,enrich_failed}'"
_SUPERSEDED = "dialpad_call_metadata #>> '{backfill,superseded_dialpad_link}'"


def eval_id_from_link(link: str | None) -> str:
    """Trailing path segment of a Dialpad link — [LONG CALL] suffixes and
    query strings stripped (mirror of team_stats._parse_row)."""
    if not link:
        return ""
    clean = link.split("[")[0].strip().split("?")[0].strip()
    return clean.rstrip("/").split("/")[-1]


def recording_urls_from_details(details: dict) -> dict:
    """§3.4.2 normalized shape: {"audio": [...], "screen": [...]}."""
    audio = [r.get("url") for r in details.get("recording_details") or [] if r.get("url")]
    screen = [u for u in details.get("screen_recording_urls") or [] if u]
    return {"audio": audio, "screen": screen}


def enrichment_fields(details: dict) -> dict:
    """Pure mapping: get_call_details payload → qa.evaluations columns."""
    duration = details.get("duration") or details.get("total_duration") or None
    return {
        "dialpad_call_id": details.get("call_id") or None,
        "dialpad_master_call_id": details.get("master_call_id") or details.get("call_id") or None,
        "dialpad_entry_point_call_id": details.get("entry_point_call_id") or None,
        "call_connected_at": _epoch_ms_to_utc_datetime(
            (details.get("raw") or {}).get("date_connected")),
        "call_started_at": _epoch_ms_to_utc_datetime(
            (details.get("raw") or {}).get("date_started")),
        "call_ended_at": _epoch_ms_to_utc_datetime(
            (details.get("raw") or {}).get("date_ended")),
        "call_duration_ms": int(duration) if duration else None,
        "call_type": details.get("direction") or None,
        "mos_score": details.get("mos_score"),
        "caller_name": details.get("caller_name") or None,
        "caller_phone": details.get("caller_phone") or None,
        "recording_urls": recording_urls_from_details(details),
    }


async def fetch_details(call_id: str, counts: dict) -> dict | None:
    """One rate-limited lookup. Returns None on deterministic empty/404.
    Raises DialpadRateLimited after backoff+retry both hit 429 — the
    caller aborts the batch gracefully."""
    try:
        details = await get_call_details(call_id)
    except DialpadRateLimited:
        counts["rate_limit_hits"] += 1
        print(f"  rate-limited on {call_id}; backing off {RATE_LIMIT_BACKOFF}s...")
        await asyncio.sleep(RATE_LIMIT_BACKOFF)
        details = await get_call_details(call_id)  # second 429 propagates
    await asyncio.sleep(RATE_LIMIT_SECONDS)
    if not details.get("call_id"):
        return None
    return details


async def phase_repair(conn, team_id: str, staging_dir: Path,
                       counts: dict, failures: list, dry_run: bool) -> None:
    """Phase R: import the clock-blocked staging rows via Dialpad repair."""
    staging_path = staging_dir / f"staging_{team_id}.jsonl"
    if not staging_path.exists():
        print(f"  phase R: no staging file at {staging_path} — skipping")
        return
    blocked = [json.loads(line) for line in staging_path.open(encoding="utf-8")]
    blocked = [s for s in blocked if s["import_blocked"]]
    counts["repair_candidates"] = len(blocked)
    if not blocked:
        return

    existing = {
        r[0] for r in await conn.fetch(
            f"SELECT {_NK} FROM qa.evaluations WHERE team_id = $1 AND {_NK} IS NOT NULL",
            team_id)
    }
    for s in blocked:
        if s["natural_key"] in existing:
            counts["repair_already_imported"] += 1
            continue
        link = s["dialpad_link"] or s["annotations"].get("superseded_dialpad_link")
        call_id = eval_id_from_link(link)
        if not call_id:
            counts["repair_unresolvable"] += 1
            failures.append({"phase": "R", "natural_key": s["natural_key"],
                             "error": "no dialpad link to repair from"})
            continue
        details = await fetch_details(call_id, counts)
        connected = enrichment_fields(details)["call_connected_at"] if details else None
        if connected is None:
            counts["repair_unresolvable"] += 1
            failures.append({"phase": "R", "natural_key": s["natural_key"],
                             "error": f"dialpad empty/404 or no date_connected for {call_id}"})
            continue
        if dry_run:
            counts["repaired_imported"] += 1
            continue

        fields = enrichment_fields(details)
        now_iso = datetime.now(timezone.utc).isoformat()
        s = dict(s)
        s["call_connected_at"] = fields["call_connected_at"].isoformat()
        s["approved_at"] = s["call_connected_at"]  # best available clock
        s["annotations"] = {
            **s["annotations"],
            "backfill_approved_at_fallback": "dialpad_repair",
            "enriched_at": now_iso,
        }
        metadata = {"backfill": {**s["annotations"],
                                 "natural_key": s["natural_key"],
                                 "staged_line": s["line"],
                                 "imported_at": now_iso},
                    "dialpad_raw": details.get("raw") or {}}
        era_ai = s["era"] == "ai"
        try:
            async with conn.transaction():
                evaluation_id = await conn.fetchval(
                    b1._EVAL_INSERT,
                    s["team_id"], s["agent_name_raw"], s["agent_email"],
                    s["evaluator_email"], s["state"], s["source"],
                    fields["call_connected_at"], s["dialpad_link"],
                    s["overall_score"], s["formula_version"], s["rubric_version"],
                    json.dumps(s["models_used"]),
                    "gemini" if era_ai else None,
                    s["key_strengths"], s["opportunities"], s["call_summary"],
                    s["caller_name"] or fields["caller_name"],
                    s["caller_phone"] or fields["caller_phone"],
                    json.dumps(metadata),
                    b1._ts(s["approved_at"]), b1._ts(s["approved_at"]),
                )
                await conn.executemany(b1._SECTION_INSERT,
                                       b1._section_params(evaluation_id, s))
                await conn.execute(
                    "UPDATE qa.evaluations SET dialpad_call_id=$2, "
                    "dialpad_master_call_id=$3, dialpad_entry_point_call_id=$4, "
                    "call_started_at=$5, call_ended_at=$6, call_duration_ms=$7, "
                    "call_type=$8, mos_score=$9, recording_urls=$10::jsonb "
                    "WHERE id=$1",
                    evaluation_id, fields["dialpad_call_id"],
                    fields["dialpad_master_call_id"],
                    fields["dialpad_entry_point_call_id"],
                    fields["call_started_at"], fields["call_ended_at"],
                    fields["call_duration_ms"], fields["call_type"],
                    fields["mos_score"], json.dumps(fields["recording_urls"]),
                )
            counts["repaired_imported"] += 1
        except Exception as e:  # noqa: BLE001
            counts["repair_failed"] += 1
            failures.append({"phase": "R", "natural_key": s["natural_key"],
                             "error": f"{type(e).__name__}: {e}"})


async def phase_enrich(conn, team_id: str, batch_size: int,
                       counts: dict, failures: list, dry_run: bool) -> None:
    """Phase E: cursor-based enrichment of imported backfill rows."""
    rows = await conn.fetch(
        f"SELECT id, dialpad_link, caller_name, caller_phone, "
        f"call_connected_at, dialpad_call_metadata, "
        f"COALESCE(dialpad_link, {_SUPERSEDED}) AS effective_link "
        f"FROM qa.evaluations "
        f"WHERE team_id = $1 AND {_NK} IS NOT NULL "
        f"AND {_ENRICHED} IS NULL AND {_FAILED} IS NULL "
        f"AND COALESCE(dialpad_link, {_SUPERSEDED}) IS NOT NULL "
        f"ORDER BY id LIMIT $2", team_id, batch_size)
    counts["enrich_batch"] = len(rows)
    counts["enrich_remaining_after"] = max(0, await conn.fetchval(
        f"SELECT COUNT(*) FROM qa.evaluations "
        f"WHERE team_id = $1 AND {_NK} IS NOT NULL "
        f"AND {_ENRICHED} IS NULL AND {_FAILED} IS NULL "
        f"AND COALESCE(dialpad_link, {_SUPERSEDED}) IS NOT NULL", team_id) - len(rows))
    counts["unenrichable_no_link"] = await conn.fetchval(
        f"SELECT COUNT(*) FROM qa.evaluations "
        f"WHERE team_id = $1 AND {_NK} IS NOT NULL "
        f"AND COALESCE(dialpad_link, {_SUPERSEDED}) IS NULL", team_id)

    for i, row in enumerate(rows, start=1):
        call_id = eval_id_from_link(row["effective_link"])
        metadata = json.loads(row["dialpad_call_metadata"])
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            details = await fetch_details(call_id, counts)
        except DialpadRateLimited:
            counts["aborted_rate_limited"] = True
            print(f"  sustained rate-limiting at row {i}/{len(rows)} — "
                  f"aborting batch gracefully; cursor resumes next run")
            break

        if details is None:
            counts["enrich_failed_404"] += 1
            if not dry_run:
                metadata["backfill"]["enrich_failed"] = f"dialpad_empty_or_404:{call_id}"
                await conn.execute(
                    "UPDATE qa.evaluations SET dialpad_call_metadata=$2::jsonb WHERE id=$1",
                    row["id"], json.dumps(metadata))
            continue

        fields = enrichment_fields(details)
        if dry_run:
            counts["enriched"] += 1
            continue
        metadata["backfill"]["enriched_at"] = now_iso
        if (fields["call_connected_at"] and row["call_connected_at"]
                and fields["call_connected_at"] != row["call_connected_at"]):
            metadata["backfill"]["pre_enrich_call_connected_at"] = (
                row["call_connected_at"].isoformat())
            counts["clock_overwrites"] += 1
        metadata["dialpad_raw"] = details.get("raw") or {}
        try:
            await conn.execute(
                "UPDATE qa.evaluations SET "
                "dialpad_call_id=$2, dialpad_master_call_id=$3, "
                "dialpad_entry_point_call_id=$4, "
                "call_connected_at=COALESCE($5, call_connected_at), "
                "call_started_at=$6, call_ended_at=$7, call_duration_ms=$8, "
                "call_type=$9, mos_score=$10, recording_urls=$11::jsonb, "
                "caller_name=COALESCE(caller_name, $12), "
                "caller_phone=COALESCE(caller_phone, $13), "
                "dialpad_call_metadata=$14::jsonb "
                "WHERE id=$1",
                row["id"], fields["dialpad_call_id"],
                fields["dialpad_master_call_id"],
                fields["dialpad_entry_point_call_id"],
                fields["call_connected_at"], fields["call_started_at"],
                fields["call_ended_at"], fields["call_duration_ms"],
                fields["call_type"], fields["mos_score"],
                json.dumps(fields["recording_urls"]),
                fields["caller_name"], fields["caller_phone"],
                json.dumps(metadata))
            counts["enriched"] += 1
            if fields["caller_name"] and not row["caller_name"]:
                counts["caller_fields_filled"] += 1
        except Exception as e:  # noqa: BLE001
            counts["enrich_failed_other"] += 1
            failures.append({"phase": "E", "id": row["id"],
                             "error": f"{type(e).__name__}: {e}"})
        if i % 50 == 0:
            print(f"  {i}/{len(rows)} enriched "
                  f"({counts['enrich_failed_404']} 404s so far)")


async def run(args) -> int:
    import asyncpg

    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn:
        print("structural: DATABASE_URL not set")
        return 1
    if not os.getenv("DIALPAD_API_KEY"):
        print("structural: DIALPAD_API_KEY not set")
        return 1

    counts = {k: 0 for k in (
        "repair_candidates", "repair_already_imported", "repaired_imported",
        "repair_unresolvable", "repair_failed", "enrich_batch", "enriched",
        "enrich_failed_404", "enrich_failed_other", "clock_overwrites",
        "caller_fields_filled", "rate_limit_hits", "unenrichable_no_link",
        "enrich_remaining_after")}
    counts["aborted_rate_limited"] = False
    failures: list[dict] = []

    conn = await asyncpg.connect(dsn, timeout=15)
    try:
        await phase_repair(conn, args.team_id, Path(args.staging_dir),
                           counts, failures, args.dry_run)
        await phase_enrich(conn, args.team_id, args.batch_size,
                           counts, failures, args.dry_run)
    finally:
        await conn.close()

    report = {"stage": "B2", "team_id": args.team_id,
              "dry_run": args.dry_run, "counts": counts, "failures": failures}
    report_path = Path(args.staging_dir) / f"report_{args.team_id}_b2.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    tag = " [DRY RUN]" if args.dry_run else ""
    print(f"[B2:{args.team_id}]{tag} repair: {counts['repaired_imported']}/"
          f"{counts['repair_candidates']} imported "
          f"({counts['repair_unresolvable']} unresolvable, "
          f"{counts['repair_already_imported']} already in)")
    print(f"  enrich: {counts['enriched']}/{counts['enrich_batch']} this batch, "
          f"{counts['enrich_failed_404']} marked 404, "
          f"{counts['clock_overwrites']} clock overwrites, "
          f"{counts['caller_fields_filled']} caller fields filled")
    print(f"  remaining after this run: {counts['enrich_remaining_after']} "
          f"(no-link unenrichable: {counts['unenrichable_no_link']})")
    print(f"  report: {report_path}")

    clean = (not counts["aborted_rate_limited"] and counts["repair_failed"] == 0
             and counts["enrich_failed_other"] == 0)
    return 0 if clean else 2


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--team-id", required=True, choices=sorted(b1.FORMULA_VERSION))
    ap.add_argument("--staging-dir", default=str(_STAGING_DIR))
    ap.add_argument("--batch-size", type=int, default=400,
                    help="Max Dialpad lookups in phase E per run (default 400)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Lookups run, nothing is written")
    args = ap.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())

"""Shadow-week report — TwoStageScoringDesign §6 (PulpoConnection §6.1 pattern).

Aggregates the two_stage_shadow stamps that score_call persists to
dialpad_call_metadata during SCORING_PIPELINE=two_stage_shadow: per-
section agreement between the served single-stage scorecard and the
Stage-B judge, judge errors, and latency. Markdown to stdout — paste
into the PR / owner review that decides the two_stage flip.

Usage (from qa-automation/AI-Scoring, .env loaded, DATABASE_URL set):

    .venv/bin/python scripts/two_stage_shadow_report.py
    .venv/bin/python scripts/two_stage_shadow_report.py --team member_support --days 7
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import asyncpg  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--team", default="member_support")
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL not set", file=sys.stderr)
        return 1

    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            """
            SELECT id, dialpad_call_id, created_at,
                   dialpad_call_metadata->'two_stage_shadow' AS stamp
            FROM qa.evaluations
            WHERE team_id = $1
              AND dialpad_call_metadata ? 'two_stage_shadow'
              AND created_at >= NOW() - make_interval(days => $2)
            ORDER BY created_at
            """,
            args.team, args.days,
        )
    finally:
        await conn.close()

    stamps = []
    for r in rows:
        stamp = r["stamp"]
        stamps.append((r, json.loads(stamp) if isinstance(stamp, str) else stamp))

    ok = [(r, s) for r, s in stamps if "error" not in s]
    errored = [(r, s) for r, s in stamps if "error" in s]

    print(f"# Two-stage shadow report — {args.team}, last {args.days}d")
    print(f"\nShadowed evals: **{len(stamps)}** "
          f"(judged: {len(ok)}, judge errors: {len(errored)})")
    if not ok:
        for r, s in errored[:10]:
            print(f"- eval {r['id']} call {r['dialpad_call_id']}: {s['error']}")
        return 0

    judges = Counter(f"{s.get('scorer_provider')}/{s.get('scorer_model')}"
                     for _, s in ok)
    print("Judges: " + ", ".join(f"{j} ×{n}" for j, n in judges.most_common()))
    latencies = sorted(s.get("elapsed_s", 0) for _, s in ok)
    print(f"Judge latency: median {latencies[len(latencies) // 2]}s, "
          f"max {latencies[-1]}s")

    # Per-section disagreement rate (mismatched_section_ids vs sections seen)
    seen: Counter = Counter()
    mismatched: Counter = Counter()
    examples: dict[str, list] = defaultdict(list)
    for r, s in ok:
        for sid in s.get("sections", {}):
            seen[sid] += 1
        for sid in s.get("mismatched_section_ids", []):
            mismatched[sid] += 1
            if len(examples[sid]) < 3:
                examples[sid].append(str(r["dialpad_call_id"]))

    fully_agreeing = sum(1 for _, s in ok if not s.get("mismatched_section_ids"))
    print(f"Scorecards in full agreement: {fully_agreeing}/{len(ok)} "
          f"({100 * fully_agreeing / len(ok):.0f}%)\n")
    print("| section | disagree | of | rate | example calls |")
    print("|---|---|---|---|---|")
    for sid, total in sorted(seen.items(), key=lambda kv: -mismatched[kv[0]] / kv[1]):
        bad = mismatched[sid]
        print(f"| {sid} | {bad} | {total} | {100 * bad / total:.0f}% | "
              f"{', '.join(examples[sid]) or '—'} |")

    if errored:
        print(f"\n## Judge errors ({len(errored)})")
        for r, s in errored[:10]:
            print(f"- eval {r['id']} call {r['dialpad_call_id']}: {s['error']}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

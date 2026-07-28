"""Shadow-week report — TwoStageScoringDesign §6 (PulpoConnection §6.1 pattern).

Aggregates the two_stage_shadow stamps that score_call persists to
dialpad_call_metadata during SCORING_PIPELINE=two_stage_shadow —
**split per judge**, because the shadow design is two one-variable
comparisons: Gemini-judge rows isolate the SPLIT's effect on scores,
Claude-judge rows isolate the MODEL's. Blending them measures nothing.

Error stamps persist with the eval row, so errors from since-fixed
deploys keep appearing for old evals — each error line carries the
eval's date so fossils are recognizable.

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


def _print_judge_section(judge: str, entries: list) -> None:
    print(f"\n## Judge: {judge} — {len(entries)} evals")

    latencies = sorted(s.get("elapsed_s", 0) for _, s in entries)
    print(f"Latency: median {latencies[len(latencies) // 2]}s, "
          f"max {latencies[-1]}s")

    fully_agreeing = sum(
        1 for _, s in entries if not s.get("mismatched_section_ids")
    )
    print(f"Full agreement with the served scorecard: "
          f"{fully_agreeing}/{len(entries)} "
          f"({100 * fully_agreeing / len(entries):.0f}%)\n")

    seen: Counter = Counter()
    mismatched: Counter = Counter()
    examples: dict[str, list] = defaultdict(list)
    for r, s in entries:
        for sid in s.get("sections", {}):
            seen[sid] += 1
        for sid in s.get("mismatched_section_ids", []):
            mismatched[sid] += 1
            if len(examples[sid]) < 3:
                examples[sid].append(str(r["dialpad_call_id"]))

    print("| section | disagree | of | rate | example calls |")
    print("|---|---|---|---|---|")
    for sid, total in sorted(seen.items(),
                             key=lambda kv: -mismatched[kv[0]] / kv[1]):
        bad = mismatched[sid]
        print(f"| {sid} | {bad} | {total} | {100 * bad / total:.0f}% | "
              f"{', '.join(examples[sid]) or '—'} |")


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

    # One section per judge — the two clean one-variable comparisons.
    by_judge: dict[str, list] = defaultdict(list)
    for r, s in ok:
        by_judge[f"{s.get('scorer_provider')}/{s.get('scorer_model')}"].append((r, s))
    for judge in sorted(by_judge):
        _print_judge_section(judge, by_judge[judge])

    if errored:
        print(f"\n## Judge errors ({len(errored)})")
        print("(stamps persist with the eval — errors dated before a fix "
              "deployed are fossils, not live failures)")
        for r, s in errored[:15]:
            when = r["created_at"].strftime("%Y-%m-%d %H:%M")
            print(f"- {when} eval {r['id']} call {r['dialpad_call_id']}: "
                  f"{s['error']}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

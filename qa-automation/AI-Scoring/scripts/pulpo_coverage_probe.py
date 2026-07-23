#!/usr/bin/env python3
"""P3 — SOP coverage probe (PulpoConnection §6.1).

One query per disposition-taxonomy label observed in
command_center.calls, batched ≤20 against the RAG provider on the
BENCHMARK token (separate quota pool): reports each label's best hit +
score, split above/below τ. The report is the pre-flip acceptance
artifact (τ calibration sample + curation gap worklist) — record the
summary in PulpoConnection.md.

Read-only against both Postgres and Pulpo.

Usage:
    cd qa-automation/AI-Scoring
    python3 scripts/pulpo_coverage_probe.py [--team-id member_support]
        [--threshold 0.55] [--markdown out.md]
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

from backend.services.rag.pulpo import build_from_env  # noqa: E402
from backend.services.rag.sop_retrieval import build_sop_query  # noqa: E402

# 60 queries/min per token and the limiter rejects a batch that exceeds
# the REMAINING minute allowance — smaller batches + pacing beat big
# bursts (observed live 2026-07-22).
BATCH = 10
BATCH_PAUSE_S = 12
RATE_LIMIT_WAIT_S = 65


async def _labels(team_id: str) -> list[tuple[str, str | None, int]]:
    """(category, subdisposition, call_count) observed in cc.calls."""
    import asyncpg
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        rows = await conn.fetch(
            """
            SELECT disposition_category, disposition, COUNT(*) AS n
            FROM command_center.calls
            WHERE team_id = $1 AND disposition_category IS NOT NULL
            GROUP BY 1, 2 ORDER BY n DESC
            """,
            team_id,
        )
    finally:
        await conn.close()
    return [(r["disposition_category"], r["disposition"], r["n"]) for r in rows]


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--team-id", default="member_support")
    parser.add_argument("--threshold", type=float, default=0.55)
    parser.add_argument("--markdown", default=None,
                        help="also write the full table to this file")
    args = parser.parse_args()

    provider = build_from_env("PULPO_MCP_BENCHMARK_TOKEN")
    if provider is None:
        print("ERROR: PULPO_MCP_BENCHMARK_TOKEN / PULPO_MCP_URL not set")
        return 1

    labels = await _labels(args.team_id)
    if not labels:
        print("No disposition labels found in command_center.calls")
        return 1
    queries = [build_sop_query(c, s, "") for c, s, _ in labels]
    print(f"{len(labels)} distinct labels → {len(queries)} queries "
          f"({(len(queries) + BATCH - 1) // BATCH} batches, rerank off)")

    hits_per_query: list[list] = []
    for i in range(0, len(queries), BATCH):
        chunk = queries[i:i + BATCH]
        try:
            hits_per_query.extend(await provider.search(chunk, limit=3))
        except Exception as e:
            if "rate_limited" not in str(e):
                raise
            print(f"  rate-limited — waiting {RATE_LIMIT_WAIT_S}s (full window) and retrying")
            await asyncio.sleep(RATE_LIMIT_WAIT_S)
            hits_per_query.extend(await provider.search(chunk, limit=3))
        print(f"  batch {i // BATCH + 1} done")
        if i + BATCH < len(queries):
            await asyncio.sleep(BATCH_PAUSE_S)
    await provider.aclose()

    rows = []
    for (category, sub, n), query, hits in zip(labels, queries, hits_per_query):
        best = hits[0] if hits else None
        rows.append({
            "label": query,
            "calls": n,
            "score": best.score if best else 0.0,
            "title": best.title if best else "(no hits)",
            "covered": bool(best and best.score >= args.threshold),
        })

    covered = [r for r in rows if r["covered"]]
    uncovered = [r for r in rows if not r["covered"]]
    call_total = sum(r["calls"] for r in rows)
    call_covered = sum(r["calls"] for r in covered)

    lines = [
        f"# Pulpo SOP coverage probe — {args.team_id}, τ={args.threshold}",
        "",
        f"Labels covered: {len(covered)}/{len(rows)} "
        f"({100 * len(covered) / len(rows):.0f}%)  ·  "
        f"call-weighted: {call_covered}/{call_total} "
        f"({100 * call_covered / call_total:.0f}%)",
        "",
        "| best score | calls | disposition label | best hit |",
        "|---|---|---|---|",
    ]
    for r in sorted(rows, key=lambda r: -r["score"]):
        mark = "" if r["covered"] else " ⚠"
        lines.append(
            f"| {r['score']:.3f}{mark} | {r['calls']} | {r['label']} | {r['title']} |"
        )
    report = "\n".join(lines)
    print("\n" + report)
    if args.markdown:
        Path(args.markdown).write_text(report + "\n", encoding="utf-8")
        print(f"\nwritten to {args.markdown}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

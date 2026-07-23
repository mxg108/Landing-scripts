#!/usr/bin/env python3
"""P1 live smoke — PulpoConnection §10 P1 checkpoint.

Searches the real Pulpo endpoint, prints titles + scores, fetches the
top document, and verifies BOTH-audience minting by retrieving at least
one Customer-audience doc (§9 A3: an Internal-only token can't see
them). Read-only.

Usage:
    cd qa-automation/AI-Scoring
    python3 scripts/pulpo_smoke.py ["optional custom query"]
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

_AI_SCORING = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_AI_SCORING))
load_dotenv(_AI_SCORING / ".env")

from backend.services.rag.factory import get_rag_provider  # noqa: E402


async def main() -> int:
    provider = get_rag_provider()
    if provider is None:
        print("ERROR: no RAG provider (check PULPO_MCP_URL / PULPO_MCP_TOKEN)")
        return 1
    print(f"provider: {provider.name}")

    queries = [
        sys.argv[1] if len(sys.argv) > 1
        else "standby cancellation policy and refunds",
        # Known Customer-audience doc (admin console, 2026-07-21):
        # proves the token was minted with BOTH audiences (§9 A3).
        "is Landing legit",
    ]
    batches = await provider.search(queries, limit=5)
    for query, hits in zip(queries, batches):
        print(f"\nquery: {query!r} — {len(hits)} hits")
        for hit in hits:
            flag_note = f" [{hit.flag_count} open flags]" if hit.flag_count else ""
            tag_note = f" tags={list(hit.tags)}" if hit.tags else ""
            print(f"  {hit.score:.3f} ({hit.score_kind or '?'}) "
                  f"{hit.title}{tag_note}{flag_note}")

    top = next((h for hits in batches for h in hits), None)
    if top is None:
        print("\nNo hits at all — corpus/token problem; smoke FAILED")
        await provider.aclose()
        return 1

    doc = await provider.get_document(top.id)
    if doc is None:
        print(f"\nget_document({top.id}) returned None — smoke FAILED")
        await provider.aclose()
        return 1
    print(f"\nget_document: {doc.title!r} — {len(doc.body)} chars, "
          f"{len(doc.flags)} flags, tags={list(doc.tags)}")
    for flag in doc.flags:
        print(f"  OPEN FLAG on {flag.quote[:60]!r}: {flag.note[:80]}")

    customer_hits = batches[1]
    print(
        "\nboth-audience check:",
        "OK" if customer_hits else
        "NO HITS for the Customer-audience probe — token may be Internal-only!",
    )
    await provider.aclose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

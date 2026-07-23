"""SOP retrieval policy — PulpoConnection §4.2 (provider-agnostic).

At Stage 1, alongside the CC grounding fetch: build a query from the
verified disposition (the C3 retrieval key; transcript head as the
absence fallback), search the RAG provider, threshold, fetch full
bodies, and render the SOP context block + provenance stamps.

Gated by PULPO_SOP_MODE (off | shadow | on, default OFF):
  off     no retrieval at all; the prompt takes the
          sop_context_missing conservative path
  shadow  retrieve + log the would-be block + stamp provenance; the
          prompt still takes the conservative path (log-only compare)
  on      the rendered block IS the prompt's SOP context; empty/sub-τ
          retrieval falls through to sop_context_missing

Never raises: any provider failure logs and returns the empty context —
retrieval is an enhancement, never a scoring blocker (the cc_context
doctrine).
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional

from backend.services.rag.factory import get_rag_provider
from backend.services.rag.provider import RagDoc, RagHit

logger = logging.getLogger(__name__)

MAX_DOCS = 3                 # inject at most this many docs
BLOCK_CHAR_CAP = 16_000      # ≈4k tokens; truncate lowest-scoring first
CACHE_TTL_S = 3600           # per-query + per-doc TTL (finite label space
                             # → steady state is a handful of queries/day)
DEFAULT_THRESHOLD = 0.55     # τ — calibrated by the §6.1 coverage probe

_search_cache: dict[str, tuple[float, list[RagHit]]] = {}
_doc_cache: dict[str, tuple[float, Optional[RagDoc]]] = {}


def pulpo_sop_mode() -> str:
    """`off` | `shadow` | `on` (unknown values collapse to `off` —
    retrieval is opt-in, unlike CC grounding's shadow default)."""
    mode = os.environ.get("PULPO_SOP_MODE", "off").strip().lower()
    return mode if mode in ("off", "shadow", "on") else "off"


def score_threshold() -> float:
    raw = os.environ.get("PULPO_SCORE_THRESHOLD", "").strip()
    try:
        return float(raw) if raw else DEFAULT_THRESHOLD
    except ValueError:
        return DEFAULT_THRESHOLD


@dataclass
class SopContext:
    """What retrieval produced for one call. Empty (`docs=[]`) means the
    prompt takes the sop_context_missing conservative path."""
    query: str = ""
    sop_title: str = ""          # top doc title — existing analytics slot
    block_text: str = ""         # rendered multi-doc SOP context
    provenance: list[dict] = field(default_factory=list)
    skipped_reason: str = ""     # '' when retrieval ran and hit


def build_sop_query(
    disposition_category: Optional[str],
    disposition: Optional[str],
    transcript_text: str,
) -> Optional[str]:
    """The disposition IS the query (§3.3); absence falls back to the
    transcript head; nothing at all → None (skip retrieval)."""
    if disposition_category:
        if disposition:
            return f"{disposition_category} — {disposition}"
        return disposition_category
    head = (transcript_text or "").strip()[:600]
    return head or None


def render_sop_block(docs: list[tuple[RagDoc, RagHit]]) -> str:
    """Pure renderer: numbered docs (title + body), flag cautions per
    Pulpo's citation-warning convention, lowest-scoring truncated first
    to honor BLOCK_CHAR_CAP."""
    if not docs:
        return ""
    sections: list[str] = []
    for i, (doc, hit) in enumerate(docs, start=1):
        lines = [f"[SOP {i}] {doc.title}"]
        for flag in doc.flags:
            lines.append(
                f"  NOTE: the passage \"{flag.quote[:120]}\" is under "
                f"review — verify before treating it as current policy."
            )
        lines.append(doc.body.strip())
        sections.append("\n".join(lines))

    # Enforce the cap from the lowest-scoring doc up: truncate the tail
    # once, then drop it if the block is still over. The retruncate
    # guard (2_100) sits ABOVE the truncated size (~2_011) so a section
    # is never re-truncated to the same length — that exact loop hung
    # the first test run.
    while sections and sum(len(s) for s in sections) > BLOCK_CHAR_CAP:
        if len(sections[-1]) > 2_100:
            sections[-1] = sections[-1][:2_000] + "\n[truncated]"
        else:
            sections.pop()
    return "\n\n".join(sections)


def _cache_get(cache: dict, key: str):
    entry = cache.get(key)
    if entry and entry[0] > time.monotonic():
        return entry[1]
    cache.pop(key, None)
    return None


def _cache_put(cache: dict, key: str, value) -> None:
    cache[key] = (time.monotonic() + CACHE_TTL_S, value)


def reset_caches_for_tests() -> None:
    _search_cache.clear()
    _doc_cache.clear()


async def fetch_sop_context(
    *,
    disposition_category: Optional[str],
    disposition: Optional[str],
    transcript_text: str,
) -> SopContext:
    """Search → threshold → fetch bodies → render. Never raises."""
    query = build_sop_query(disposition_category, disposition, transcript_text)
    if query is None:
        return SopContext(skipped_reason="no_query_material")

    provider = get_rag_provider()
    if provider is None:
        return SopContext(query=query, skipped_reason="no_provider")

    try:
        hits = _cache_get(_search_cache, query)
        if hits is None:
            (hits,) = await provider.search([query], limit=5)
            _cache_put(_search_cache, query, hits)

        tau = score_threshold()
        selected = [h for h in hits if h.score >= tau][:MAX_DOCS]
        if not selected:
            return SopContext(query=query, skipped_reason="below_threshold")

        docs: list[tuple[RagDoc, RagHit]] = []
        for hit in selected:
            doc = _cache_get(_doc_cache, hit.id)
            if doc is None:
                doc = await provider.get_document(hit.id)
                _cache_put(_doc_cache, hit.id, doc)
            if doc is not None and doc.body.strip():
                docs.append((doc, hit))
        if not docs:
            return SopContext(query=query, skipped_reason="no_bodies")

        return SopContext(
            query=query,
            sop_title=docs[0][0].title,
            block_text=render_sop_block(docs),
            provenance=[
                {
                    "id": doc.id,
                    "title": doc.title,
                    "score": hit.score,
                    "score_kind": hit.score_kind,
                    "updated_at": doc.updated_at,
                    "open_flags": len(doc.flags),
                }
                for doc, hit in docs
            ],
        )
    except Exception:
        logger.exception(
            "sop_retrieval: failed for query=%r — scoring proceeds without SOP context",
            query,
        )
        return SopContext(query=query, skipped_reason="provider_error")

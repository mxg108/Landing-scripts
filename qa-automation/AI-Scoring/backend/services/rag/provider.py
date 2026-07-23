"""Neutral RAG-provider contract — PulpoConnection.md §4.1.

NOTHING vendor-shaped belongs here: no Pulpo field names, no MCP
concepts. Consumers (the SOP retrieval policy, the coverage probe, the
migration script) program against these types only, so a vendor swap
never touches policy code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class RagHit:
    """One search result. `score` semantics are provider-defined but
    MUST be monotonic (higher = more relevant); `score_kind` names the
    scale (e.g. 'cosine', 'rerank') for provenance, never for logic."""
    id: str
    title: str
    excerpt: str
    score: float
    score_kind: str = ""
    tags: tuple[str, ...] = ()
    updated_at: str = ""          # provider-rendered; provenance only
    flag_count: int = 0


@dataclass(frozen=True)
class RagFlag:
    """An open review flag on an exact passage of a document."""
    quote: str
    note: str = ""
    suggestion: str = ""


@dataclass(frozen=True)
class RagDoc:
    """A full document body, ready for prompt injection."""
    id: str
    title: str
    body: str
    flags: tuple[RagFlag, ...] = ()
    tags: tuple[str, ...] = ()
    updated_at: str = ""


class RagProviderError(Exception):
    """Any provider-side failure (transport, auth, malformed payload).
    Policy code catches THIS type — vendor exceptions never escape the
    vendor module."""


@dataclass
class RagUpsertResult:
    id: str
    created: bool


class RagProvider:
    """Abstract provider. `search`/`get_document` are the required
    surface; `flag_document`/`upsert_document` are OPTIONAL capabilities
    — curation flows probe with `supports_*` before calling."""

    name: str = "abstract"

    async def search(
        self, queries: list[str], *, limit: int = 5
    ) -> list[list[RagHit]]:
        """One result list PER query, order-aligned with `queries`."""
        raise NotImplementedError

    async def get_document(self, doc_id: str) -> Optional[RagDoc]:
        """Full document, or None when the id doesn't resolve."""
        raise NotImplementedError

    # -- optional capabilities -------------------------------------------
    @property
    def supports_flagging(self) -> bool:
        return False

    async def flag_document(
        self, doc_id: str, quote: str, *, note: str = "", suggestion: str = ""
    ) -> None:
        raise NotImplementedError(f"{self.name} does not support flagging")

    @property
    def supports_upsert(self) -> bool:
        return False

    async def upsert_document(
        self, *, title: str, body: str, doc_id: Optional[str] = None
    ) -> RagUpsertResult:
        raise NotImplementedError(f"{self.name} does not support upsert")

    async def aclose(self) -> None:
        """Release transport resources; lifespan teardown calls this."""
        return None

"""RAG provider factory — RAG_PROVIDER env selects the vendor module.

    pulpo   (default) PulpoProvider from PULPO_MCP_URL/PULPO_MCP_TOKEN
    none    retrieval disabled (dev/tests; also any unknown value)

Singleton per process (one httpx client, one MCP session); the FastAPI
lifespan closes it via `close_rag_provider`.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from backend.services.rag.provider import RagProvider

logger = logging.getLogger(__name__)

_provider: Optional[RagProvider] = None
_resolved = False


def get_rag_provider() -> Optional[RagProvider]:
    global _provider, _resolved
    if _resolved:
        return _provider
    mode = os.environ.get("RAG_PROVIDER", "pulpo").strip().lower()
    if mode == "pulpo":
        from backend.services.rag.pulpo import build_from_env
        _provider = build_from_env()
    elif mode != "none":
        logger.error("rag: unknown RAG_PROVIDER=%r — retrieval disabled", mode)
    _resolved = True
    return _provider


async def close_rag_provider() -> None:
    """Lifespan teardown."""
    global _provider, _resolved
    if _provider is not None:
        await _provider.aclose()
    _provider, _resolved = None, False


def reset_for_tests() -> None:
    """Drop the cached provider so tests can re-point env."""
    global _provider, _resolved
    _provider, _resolved = None, False

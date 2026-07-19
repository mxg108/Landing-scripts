"""Abstract data provider + the Postgres provider factory.

Every read serves from qa.* via a ``connect()``-ed ``PostgresProvider``
(pool open, roster snapshot loaded). The flip completed 2026-07-15; the
``QA_READ_PATH`` flag and its Sheets/shadow branches were deleted in F5
(ReadPathFlip §5) — rollback is git revert + redeploy, and the
Analyst_History sheet remains a write-side projection until retirement.
``SheetsProvider`` survives in history_service for the parity harness,
which instantiates it directly; the factory never returns it.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod

from backend.models.dashboard import EvaluationRecord

logger = logging.getLogger(__name__)


class DataProvider(ABC):
    """Abstract interface for evaluation data access."""

    name: str = "unknown"

    @abstractmethod
    async def list_agents(self) -> list[str]:
        ...

    @abstractmethod
    async def get_agent_history(self, agent_name: str, days: int = 30) -> list[EvaluationRecord]:
        ...

    @abstractmethod
    async def get_all_history(self, days: int = 90) -> list[EvaluationRecord]:
        ...

    @abstractmethod
    def _get_mails_sheet(self) -> list[list[str]]:
        ...

    async def get_by_eval_id(self, call_id: str) -> EvaluationRecord | None:
        """Fast single-eval lookup for /datapoints/{call_id} (ReadPathFlip
        §3 W6). Default: unsupported — return None and let the caller fall
        back to scanning get_all_history. PostgresProvider overrides this
        with an indexed query; SheetsProvider has no index to exploit."""
        return None


# Per-team provider cache (keyed by team_id).
_providers: dict[str, DataProvider] = {}
_build_lock = asyncio.Lock()


async def get_provider(team_id: str = "member_support") -> DataProvider:
    """Return the cached, ``connect()``-ed PostgresProvider for *team_id*
    (pool open, roster snapshot loaded), building it on first use."""
    cached = _providers.get(team_id)
    if cached is not None:
        return cached

    # Build under a lock so two concurrent first-hits don't each open a
    # Postgres pool. Re-check inside — another coroutine may have won.
    async with _build_lock:
        cached = _providers.get(team_id)
        if cached is not None:
            return cached

        from backend.config.team_config import get_team_config
        from backend.services.db_provider import PostgresProvider
        provider = PostgresProvider(config=get_team_config(team_id))
        await provider.connect()
        _providers[team_id] = provider
        logger.info("get_provider[%s] → %s", team_id, provider.name)
        return provider


async def _close_provider(provider: DataProvider) -> None:
    close = getattr(provider, "close", None)
    if close is None:
        return
    try:
        await close()
    except Exception:  # noqa: BLE001 — teardown must not raise
        logger.warning("provider %s close() failed", provider.name, exc_info=True)


async def close_all_providers() -> None:
    """Close every cached provider (Postgres pools) and clear the cache.

    Called from the app lifespan on shutdown; safe to call when the cache
    holds only SheetsProviders (which have no close())."""
    for provider in list(_providers.values()):
        await _close_provider(provider)
    _providers.clear()

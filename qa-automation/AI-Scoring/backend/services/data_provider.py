"""Abstract data provider with the Sheets↔Postgres read-path factory.

The read path is selected by the ``QA_READ_PATH`` env var (ReadPathFlip
§5), default ``sheets``:

- ``sheets``   → ``SheetsProvider`` (today's path; reads Analyst_History).
- ``postgres`` → ``PostgresProvider``, ``connect()``-ed before it is
  handed out so its pool + roster snapshot are live.

Flip and rollback are a single env change + redeploy — reads are
stateless, so no per-team column is warranted. The factory re-checks the
env on every call and rebuilds a team's provider if the mode changed, so
the flag is honored without a process restart when Railway does an
in-place env edit.

NOTE (F3 scope): this flips **Path 1** (the provider-based endpoints —
/agents, /datapoints, …). The /team analytics trio still reaches into
``SheetsProvider._ws`` directly and is NOT yet safe under
``QA_READ_PATH=postgres``; F4 makes that trio flag-aware via the Postgres
row-source. Until F4 lands, keep the prod flag on ``sheets``.
"""

from __future__ import annotations

import asyncio
import logging
import os
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


# Per-team provider cache (keyed by team_id) + the read-path mode each
# cached instance was built for, so a runtime flag flip rebuilds cleanly.
_providers: dict[str, DataProvider] = {}
_provider_modes: dict[str, str] = {}
_build_lock = asyncio.Lock()


def _read_path_mode() -> str:
    """Current read-path selection — ``postgres`` or ``sheets`` (default)."""
    return os.environ.get("QA_READ_PATH", "sheets").strip().lower()


async def get_provider(team_id: str = "member_support") -> DataProvider:
    """Return a cached, ready-to-use provider for *team_id*.

    On the ``postgres`` path the returned instance is already
    ``connect()``-ed (pool open, roster snapshot loaded). Reuses the same
    instance across requests; rebuilds only when ``QA_READ_PATH`` changed
    since the instance was cached.
    """
    mode = _read_path_mode()
    cached = _providers.get(team_id)
    if cached is not None and _provider_modes.get(team_id) == mode:
        return cached

    # Build under a lock so two concurrent first-hits don't each open a
    # Postgres pool. Re-check inside — another coroutine may have won.
    async with _build_lock:
        cached = _providers.get(team_id)
        if cached is not None and _provider_modes.get(team_id) == mode:
            return cached

        from backend.config.team_config import get_team_config
        config = get_team_config(team_id)

        if mode == "postgres":
            from backend.services.db_provider import PostgresProvider
            provider: DataProvider = PostgresProvider(config=config)
            await provider.connect()
        else:
            from backend.services.history_service import SheetsProvider
            provider = SheetsProvider(config=config)

        previous = _providers.get(team_id)
        _providers[team_id] = provider
        _provider_modes[team_id] = mode
        logger.info("get_provider[%s] → %s (QA_READ_PATH=%s)",
                    team_id, provider.name, mode)

        # Best-effort close of a superseded provider (e.g. a Postgres pool
        # left over from a mode flip) so we don't leak connections.
        if previous is not None and previous is not provider:
            await _close_provider(previous)
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
    _provider_modes.clear()

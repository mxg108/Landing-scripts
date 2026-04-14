"""Abstract data provider with factory for Sheets/Postgres fallback."""

from __future__ import annotations

from abc import ABC, abstractmethod
from backend.models.dashboard import EvaluationRecord


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


# Per-team provider cache (keyed by team_id)
_providers: dict[str, DataProvider] = {}


async def get_provider(team_id: str = "member_support") -> DataProvider:
    """Return a cached SheetsProvider for the given team.

    Reuses the same instance (and its sheet cache) across requests.
    """
    if team_id not in _providers:
        from backend.config.team_config import get_team_config
        from backend.services.history_service import SheetsProvider
        config = get_team_config(team_id)
        _providers[team_id] = SheetsProvider(config=config)
    return _providers[team_id]

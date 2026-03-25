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


_provider_instance: DataProvider | None = None


async def get_provider() -> DataProvider:
    """Return a singleton SheetsProvider. Reuses the same instance (and its cache) across requests."""
    global _provider_instance
    if _provider_instance is None:
        from backend.services.history_service import SheetsProvider
        _provider_instance = SheetsProvider()
    return _provider_instance

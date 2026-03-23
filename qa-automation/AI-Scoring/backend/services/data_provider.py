"""Abstract data provider with factory for Sheets/Postgres fallback."""

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


async def get_provider() -> DataProvider:
    """Use Sheets as the primary provider. Postgres is optional and unreliable on free tier."""
    from backend.services.history_service import SheetsProvider
    return SheetsProvider()

"""PostgreSQL data provider — optional accelerator."""

import os
from datetime import datetime, timedelta
from backend.models.dashboard import EvaluationRecord, SectionScore
from backend.services.data_provider import DataProvider


class PostgresProvider(DataProvider):
    """Reads evaluation data from PostgreSQL qa_scoring schema."""

    name = "PostgreSQL"

    def __init__(self):
        self._pool = None
        self._dsn = os.environ.get("DATABASE_URL", "")

    async def connect(self):
        import asyncpg
        if not self._dsn:
            raise ConnectionError("DATABASE_URL not configured")
        self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=5, timeout=3)
        async with self._pool.acquire() as conn:
            await conn.fetchval("SELECT 1")

    async def close(self):
        if self._pool:
            await self._pool.close()

    async def list_agents(self) -> list[str]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT DISTINCT agent_name FROM qa_scoring.evaluations ORDER BY agent_name"
            )
        return [r["agent_name"] for r in rows]

    async def get_agent_history(self, agent_name: str, days: int = 30) -> list[EvaluationRecord]:
        cutoff = datetime.now() - timedelta(days=days)
        async with self._pool.acquire() as conn:
            evals = await conn.fetch(
                "SELECT * FROM qa_scoring.evaluations "
                "WHERE LOWER(agent_name) = LOWER($1) AND created_at >= $2 "
                "ORDER BY created_at",
                agent_name, cutoff,
            )
            records = []
            for ev in evals:
                scores = await conn.fetch(
                    "SELECT * FROM qa_scoring.evaluation_scores WHERE evaluation_id = $1",
                    ev["id"],
                )
                sections = {}
                for sr in scores:
                    sections[sr["section_name"]] = SectionScore(
                        score=str(sr["score"]),
                        confidence=sr.get("confidence"),
                        reasoning=sr.get("reasoning"),
                    )
                records.append(EvaluationRecord(
                    timestamp=ev["created_at"],
                    agent_name=ev["agent_name"],
                    agent_email=ev.get("agent_email", ""),
                    manager_email=ev.get("manager_email", ""),
                    overall_score=float(ev["overall_score"]),
                    sections=sections,
                    key_strengths=ev.get("key_strengths"),
                    improvements=ev.get("improvements"),
                    source=ev.get("source", "manual"),
                ))
        return records

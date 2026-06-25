"""Tests for migration 005 — command_center.* tables.

Per SQLMigration.md §11.5 floor:
  - ≥1 CHECK test per declared CHECK constraint
  - ≥1 UPSERT-idempotency test per UNIQUE conflict target

Plus a runner-level integration test that applies 004+005 in sequence and
rolls back to confirm the down script leaves the database in the state
004 alone would (schemas + teams, no CC tables).
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

from database import runner

REPO_ROOT = Path(__file__).resolve().parents[4]
MIGRATIONS_DIR = REPO_ROOT / "database" / "migrations"

UP_004 = (MIGRATIONS_DIR / "004_create_schemas_and_teams.sql").read_text()
UP_005 = (MIGRATIONS_DIR / "005_command_center_tables.sql").read_text()
DOWN_005 = (MIGRATIONS_DIR / "005_command_center_tables_down.sql").read_text()


@pytest_asyncio.fixture
async def pg_005(clean_pg: asyncpg.Connection) -> asyncpg.Connection:
    """clean_pg + 004 + 005 applied. Common precondition."""
    await clean_pg.execute(UP_004)
    await clean_pg.execute(UP_005)
    return clean_pg


# ---------------------------------------------------------------------------
# webhook_events — CHECKs + partial UNIQUEs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webhook_events_event_kind_rejects_invalid_value(
    pg_005: asyncpg.Connection,
) -> None:
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await pg_005.execute(
            """
            INSERT INTO command_center.webhook_events
                (team_id, event_kind, dialpad_call_id, state,
                 event_timestamp, raw_payload)
            VALUES ('sales', 'unknown_kind', 'c1', 'connected',
                    NOW(), '{}'::jsonb)
            """
        )


@pytest.mark.asyncio
async def test_webhook_events_call_event_requires_call_id(
    pg_005: asyncpg.Connection,
) -> None:
    """event_kind='call' MUST have dialpad_call_id — otherwise the partial
    UNIQUE for call dedupe would collapse on NULL keys."""
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await pg_005.execute(
            """
            INSERT INTO command_center.webhook_events
                (team_id, event_kind, state, event_timestamp, raw_payload)
            VALUES ('sales', 'call', 'connected', NOW(), '{}'::jsonb)
            """
        )


@pytest.mark.asyncio
async def test_webhook_events_agent_status_event_without_call_id_ok(
    pg_005: asyncpg.Connection,
) -> None:
    """The call_id_when_call CHECK is conditional on event_kind — an
    agent_status event without a call_id is valid."""
    await pg_005.execute(
        """
        INSERT INTO command_center.webhook_events
            (team_id, event_kind, dialpad_agent_id, state,
             event_timestamp, raw_payload)
        VALUES ('sales', 'agent_status', 'a1', 'available',
                NOW(), '{}'::jsonb)
        """
    )
    n = await pg_005.fetchval("SELECT COUNT(*) FROM command_center.webhook_events")
    assert n == 1


@pytest.mark.asyncio
async def test_webhook_events_blank_state_rejected(
    pg_005: asyncpg.Connection,
) -> None:
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await pg_005.execute(
            """
            INSERT INTO command_center.webhook_events
                (team_id, event_kind, dialpad_call_id, state,
                 event_timestamp, raw_payload)
            VALUES ('sales', 'call', 'c1', '', NOW(), '{}'::jsonb)
            """
        )


@pytest.mark.asyncio
async def test_webhook_events_partial_unique_dedupes_call_kind(
    pg_005: asyncpg.Connection,
) -> None:
    """The (dialpad_call_id, state, event_timestamp) UNIQUE applies only
    to event_kind='call' — a replayed call payload with the same triple
    must collide."""
    ts = datetime.datetime(2026, 6, 23, 10, 0, 0, tzinfo=datetime.timezone.utc)
    await pg_005.execute(
        """
        INSERT INTO command_center.webhook_events
            (team_id, event_kind, dialpad_call_id, state,
             event_timestamp, raw_payload)
        VALUES ('sales', 'call', 'c1', 'connected', $1::timestamptz, '{}'::jsonb)
        """,
        ts,
    )
    with pytest.raises(asyncpg.exceptions.UniqueViolationError):
        await pg_005.execute(
            """
            INSERT INTO command_center.webhook_events
                (team_id, event_kind, dialpad_call_id, state,
                 event_timestamp, raw_payload)
            VALUES ('sales', 'call', 'c1', 'connected', $1::timestamptz, '{}'::jsonb)
            """,
            ts,
        )


@pytest.mark.asyncio
async def test_webhook_events_partial_unique_dedupes_agent_status(
    pg_005: asyncpg.Connection,
) -> None:
    ts = datetime.datetime(2026, 6, 23, 10, 0, 0, tzinfo=datetime.timezone.utc)
    await pg_005.execute(
        """
        INSERT INTO command_center.webhook_events
            (team_id, event_kind, dialpad_agent_id, state,
             event_timestamp, raw_payload)
        VALUES ('sales', 'agent_status', 'a1', 'busy', $1::timestamptz, '{}'::jsonb)
        """,
        ts,
    )
    with pytest.raises(asyncpg.exceptions.UniqueViolationError):
        await pg_005.execute(
            """
            INSERT INTO command_center.webhook_events
                (team_id, event_kind, dialpad_agent_id, state,
                 event_timestamp, raw_payload)
            VALUES ('sales', 'agent_status', 'a1', 'busy', $1::timestamptz, '{}'::jsonb)
            """,
            ts,
        )


@pytest.mark.asyncio
async def test_webhook_events_different_kinds_same_timestamp_both_ok(
    pg_005: asyncpg.Connection,
) -> None:
    """Two partial UNIQUEs are independent — a call event and an
    agent_status event with the same timestamp do NOT collide."""
    ts = datetime.datetime(2026, 6, 23, 10, 0, 0, tzinfo=datetime.timezone.utc)
    await pg_005.execute(
        """
        INSERT INTO command_center.webhook_events
            (team_id, event_kind, dialpad_call_id, state,
             event_timestamp, raw_payload)
        VALUES ('sales', 'call', 'c1', 'connected', $1::timestamptz, '{}'::jsonb)
        """,
        ts,
    )
    await pg_005.execute(
        """
        INSERT INTO command_center.webhook_events
            (team_id, event_kind, dialpad_agent_id, state,
             event_timestamp, raw_payload)
        VALUES ('sales', 'agent_status', 'a1', 'busy', $1::timestamptz, '{}'::jsonb)
        """,
        ts,
    )
    n = await pg_005.fetchval("SELECT COUNT(*) FROM command_center.webhook_events")
    assert n == 2


# ---------------------------------------------------------------------------
# calls — CHECKs + scored/orphan pair invariants + team_call_id UNIQUE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_calls_seen_via_rejects_invalid(
    pg_005: asyncpg.Connection,
) -> None:
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await pg_005.execute(
            """
            INSERT INTO command_center.calls
                (team_id, dialpad_call_id, seen_via)
            VALUES ('sales', 'c1', 'NOT_A_KNOWN_SOURCE')
            """
        )


@pytest.mark.asyncio
async def test_calls_scored_pair_requires_scored_at_when_true(
    pg_005: asyncpg.Connection,
) -> None:
    """If `scored=TRUE`, `scored_at` must be populated. The flag pair is
    a load-bearing invariant for the calls-received-vs-scored ratio
    (§4.2) and for the orphan-detection workflow (§3.9)."""
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await pg_005.execute(
            """
            INSERT INTO command_center.calls
                (team_id, dialpad_call_id, seen_via, scored)
            VALUES ('sales', 'c1', 'qa_on_demand', TRUE)
            """
        )


@pytest.mark.asyncio
async def test_calls_scored_pair_allows_both_set(
    pg_005: asyncpg.Connection,
) -> None:
    await pg_005.execute(
        """
        INSERT INTO command_center.calls
            (team_id, dialpad_call_id, seen_via, scored, scored_at)
        VALUES ('sales', 'c1', 'qa_on_demand', TRUE, NOW())
        """
    )
    assert (
        await pg_005.fetchval("SELECT scored FROM command_center.calls WHERE dialpad_call_id = 'c1'")
        is True
    )


@pytest.mark.asyncio
async def test_calls_orphaned_pair_requires_orphaned_at(
    pg_005: asyncpg.Connection,
) -> None:
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await pg_005.execute(
            """
            INSERT INTO command_center.calls
                (team_id, dialpad_call_id, seen_via, evaluation_orphaned)
            VALUES ('sales', 'c1', 'qa_on_demand', TRUE)
            """
        )


@pytest.mark.asyncio
async def test_calls_team_call_id_unique_blocks_duplicate(
    pg_005: asyncpg.Connection,
) -> None:
    """`uq_calls_team_call_id` is the load-bearing constraint behind the
    write paths in §4.2 (every UPSERT targets ON CONFLICT (team_id,
    dialpad_call_id))."""
    await pg_005.execute(
        """
        INSERT INTO command_center.calls
            (team_id, dialpad_call_id, seen_via)
        VALUES ('sales', 'c1', 'webhook')
        """
    )
    with pytest.raises(asyncpg.exceptions.UniqueViolationError):
        await pg_005.execute(
            """
            INSERT INTO command_center.calls
                (team_id, dialpad_call_id, seen_via)
            VALUES ('sales', 'c1', 'qa_on_demand')
            """
        )


@pytest.mark.asyncio
async def test_calls_upsert_on_team_call_id_round_trips(
    pg_005: asyncpg.Connection,
) -> None:
    """End-to-end demo of the ON CONFLICT pattern §4.2 write paths use."""
    await pg_005.execute(
        """
        INSERT INTO command_center.calls
            (team_id, dialpad_call_id, seen_via, agent_name)
        VALUES ('sales', 'c1', 'webhook', 'Alpha Agent')
        ON CONFLICT (team_id, dialpad_call_id) DO UPDATE
            SET agent_name = EXCLUDED.agent_name,
                last_updated_at = NOW()
        """
    )
    # Second writer (e.g. QA scoring path) refreshes the agent name.
    await pg_005.execute(
        """
        INSERT INTO command_center.calls
            (team_id, dialpad_call_id, seen_via, agent_name)
        VALUES ('sales', 'c1', 'qa_on_demand', 'Alpha Agent (corrected)')
        ON CONFLICT (team_id, dialpad_call_id) DO UPDATE
            SET agent_name = EXCLUDED.agent_name,
                last_updated_at = NOW()
        """
    )
    rows = await pg_005.fetch(
        "SELECT agent_name, seen_via FROM command_center.calls WHERE dialpad_call_id = 'c1'"
    )
    # `seen_via` is set on insert and not touched by the ON CONFLICT clause
    # → first writer's value wins. Application code MUST NOT include
    # seen_via in the SET list per §4.2 ("seen_via='webhook' on insert;
    # never overwritten").
    assert len(rows) == 1
    assert rows[0]["agent_name"] == "Alpha Agent (corrected)"
    assert rows[0]["seen_via"] == "webhook"


# ---------------------------------------------------------------------------
# chiclets + chiclet_events
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chiclets_type_rejects_invalid(pg_005: asyncpg.Connection) -> None:
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await pg_005.execute(
            """
            INSERT INTO command_center.chiclets
                (team_id, type, tier, status, summary)
            VALUES ('sales', 'unknown_type', 'T1', 'active', 'x')
            """
        )


@pytest.mark.asyncio
async def test_chiclets_tier_rejects_invalid(pg_005: asyncpg.Connection) -> None:
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await pg_005.execute(
            """
            INSERT INTO command_center.chiclets
                (team_id, type, tier, status, summary)
            VALUES ('sales', 'hold', 'T9', 'active', 'x')
            """
        )


@pytest.mark.asyncio
async def test_chiclets_status_rejects_invalid(pg_005: asyncpg.Connection) -> None:
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await pg_005.execute(
            """
            INSERT INTO command_center.chiclets
                (team_id, type, tier, status, summary)
            VALUES ('sales', 'hold', 'T1', 'pending', 'x')
            """
        )


@pytest.mark.asyncio
async def test_chiclets_resolved_pair_requires_resolved_at_and_by(
    pg_005: asyncpg.Connection,
) -> None:
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await pg_005.execute(
            """
            INSERT INTO command_center.chiclets
                (team_id, type, tier, status, summary)
            VALUES ('sales', 'hold', 'T1', 'resolved', 'x')
            """
        )


@pytest.mark.asyncio
async def test_chiclets_active_does_not_require_resolved_fields(
    pg_005: asyncpg.Connection,
) -> None:
    """The pair CHECK is conditional on status='resolved'. An active
    chiclet with NULL resolved_at/resolved_by is valid."""
    await pg_005.execute(
        """
        INSERT INTO command_center.chiclets
            (team_id, type, tier, status, summary, data)
        VALUES ('sales', 'hold', 'T1', 'active', 'x', '{"seconds": 45}'::jsonb)
        """
    )
    data = await pg_005.fetchval(
        "SELECT data FROM command_center.chiclets WHERE summary = 'x'"
    )
    assert json.loads(data) == {"seconds": 45}


@pytest.mark.asyncio
async def test_chiclet_events_event_type_rejects_invalid(
    pg_005: asyncpg.Connection,
) -> None:
    cid = await pg_005.fetchval(
        """
        INSERT INTO command_center.chiclets
            (team_id, type, tier, status, summary)
        VALUES ('sales', 'hold', 'T1', 'active', 'x')
        RETURNING id
        """
    )
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await pg_005.execute(
            "INSERT INTO command_center.chiclet_events (chiclet_id, event_type) "
            "VALUES ($1, 'unknown_event')",
            cid,
        )


@pytest.mark.asyncio
async def test_chiclet_events_cascade_delete_with_parent(
    pg_005: asyncpg.Connection,
) -> None:
    """ON DELETE CASCADE on chiclet_id means deleting a chiclet wipes
    its event history. (Chiclets are permanent in normal flow, but the
    cascade prevents orphan events if cleanup is ever needed.)"""
    cid = await pg_005.fetchval(
        """
        INSERT INTO command_center.chiclets
            (team_id, type, tier, status, summary)
        VALUES ('sales', 'hold', 'T1', 'active', 'x')
        RETURNING id
        """
    )
    await pg_005.execute(
        "INSERT INTO command_center.chiclet_events (chiclet_id, event_type) "
        "VALUES ($1, 'created'), ($1, 'updated')",
        cid,
    )
    await pg_005.execute("DELETE FROM command_center.chiclets WHERE id = $1", cid)
    assert (
        await pg_005.fetchval(
            "SELECT COUNT(*) FROM command_center.chiclet_events WHERE chiclet_id = $1",
            cid,
        )
        == 0
    )


# ---------------------------------------------------------------------------
# dialpad_agents — UNIQUE (team_id, dialpad_agent_id)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dialpad_agents_unique_team_agent(
    pg_005: asyncpg.Connection,
) -> None:
    await pg_005.execute(
        "INSERT INTO command_center.dialpad_agents (team_id, dialpad_agent_id, display_name) "
        "VALUES ('sales', 'a1', 'Alpha')"
    )
    # Same agent on a DIFFERENT team is fine — partial UNIQUE is per team.
    await pg_005.execute(
        "INSERT INTO command_center.dialpad_agents (team_id, dialpad_agent_id, display_name) "
        "VALUES ('member_support', 'a1', 'Alpha (MS)')"
    )
    with pytest.raises(asyncpg.exceptions.UniqueViolationError):
        await pg_005.execute(
            "INSERT INTO command_center.dialpad_agents (team_id, dialpad_agent_id, display_name) "
            "VALUES ('sales', 'a1', 'Alpha duplicate')"
        )


# ---------------------------------------------------------------------------
# Cross-table — team_id FKs reject unknown teams
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_team_id_fk_rejects_unknown_team(
    pg_005: asyncpg.Connection,
) -> None:
    with pytest.raises(asyncpg.exceptions.ForeignKeyViolationError):
        await pg_005.execute(
            """
            INSERT INTO command_center.calls
                (team_id, dialpad_call_id, seen_via)
            VALUES ('nonexistent_team', 'c1', 'webhook')
            """
        )


# ---------------------------------------------------------------------------
# Down — drops every command_center.* table cleanly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_down_drops_all_cc_tables(pg_005: asyncpg.Connection) -> None:
    await pg_005.execute(DOWN_005)
    rows = await pg_005.fetch(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'command_center'"
    )
    assert rows == []


# ---------------------------------------------------------------------------
# Runner integration — 004 then 005 then down 005
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runner_applies_004_and_005_in_sequence(
    clean_pg: asyncpg.Connection, tmp_path: Path
) -> None:
    import shutil
    migdir = tmp_path / "migrations"
    migdir.mkdir()
    for name in [
        "004_create_schemas_and_teams.sql",
        "004_create_schemas_and_teams_down.sql",
        "005_command_center_tables.sql",
        "005_command_center_tables_down.sql",
    ]:
        shutil.copy(MIGRATIONS_DIR / name, migdir)

    rc = await runner.cmd_up(clean_pg, migrations_dir=migdir)
    assert rc == 0

    applied = await clean_pg.fetch(
        "SELECT version, name FROM public.schema_migrations ORDER BY version"
    )
    assert [(r["version"], r["name"]) for r in applied] == [
        (4, "create_schemas_and_teams"),
        (5, "command_center_tables"),
    ]

    # Rolling back 005 only must leave 004's state intact.
    rc = await runner.cmd_down(clean_pg)
    assert rc == 0
    tables = await clean_pg.fetch(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'command_center'"
    )
    assert tables == []
    # public.teams still has its seed rows — 004 untouched.
    assert await clean_pg.fetchval("SELECT COUNT(*) FROM public.teams") == 2

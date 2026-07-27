"""ScorecardActionsDesign §3 (S1) — DB-backed action resolution.

`resolve_evaluation` is the seam every scorecard action stands on after
the in-memory `_jobs` dict dies with the process. These tests pin the
job-id parsing, the two-column Dialpad-id probe, and the DB-row →
Stage-1-draft-dump section reshaping against a stub pool (same FakeConn
pattern as test_eval_store — no real DB, per Wave2Plan Phase 1).
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

import pytest

from backend.services import eval_store
from backend.services.eval_store import call_id_from_job_id, resolve_evaluation


# ---------------------------------------------------------------------------
# call_id_from_job_id
# ---------------------------------------------------------------------------

class TestCallIdFromJobId:

    def test_job_id_with_agent_suffix(self):
        assert call_id_from_job_id("5035229460504576_Juan_Celso") == "5035229460504576"

    def test_bare_call_id_passes_through(self):
        assert call_id_from_job_id("5035229460504576") == "5035229460504576"

    def test_agent_name_with_many_underscores(self):
        # _make_job_id flattens spaces, so multi-word names produce
        # multiple underscores — only the first split matters.
        assert call_id_from_job_id("42_Maria_de_la_Cruz") == "42"

    def test_non_numeric_prefix_returns_empty(self):
        # Junk ids skip the DB probe entirely.
        assert call_id_from_job_id("Juan_Celso") == ""

    def test_empty_returns_empty(self):
        assert call_id_from_job_id("") == ""


# ---------------------------------------------------------------------------
# resolve_evaluation
# ---------------------------------------------------------------------------

def _eval_row(**overrides):
    row = {
        "id": 2377,
        "team_id": "member_support",
        "state": "draft",
        "source": "ai",
        "scoring_status": "flagged_human_review",
        "agent_name_raw": "Juan Celso",
        "agent_email": "juan.celso@hellolanding.com",
        "evaluator_email": None,
        "dialpad_call_id": "5035229460504576",
        "dialpad_entry_point_call_id": "6105002063634432",
        "dialpad_link": "https://dialpad.com/callhistory/callreview/6105002063634432",
        "call_summary": "Member called about billing",
        "key_strengths": "Good tone",
        "opportunities": "Faster holds",
        "overall_score": None,
        "models_used": json.dumps(
            {"text": {"provider": "gemini", "model": "gemini-2.5-flash"}}
        ),
        "agent_id": 7,
        "duration_ms": 180000,
    }
    row.update(overrides)
    return row


def _section_rows():
    return [
        {"section_id": "greeting", "score_type": "numeric", "numeric_score": 5,
         "binary_value": None, "score_source": "ai", "confidence": "HIGH",
         "reasoning": "warm open"},
        {"section_id": "caller_id", "score_type": "binary", "numeric_score": None,
         "binary_value": "Y", "score_source": "ai", "confidence": "MED",
         "reasoning": "verified"},
        # migration-012 numeric-NA shape: numeric section, binary_value='NA'
        {"section_id": "efficiency", "score_type": "numeric", "numeric_score": None,
         "binary_value": "NA", "score_source": "ai", "confidence": "LOW",
         "reasoning": "no holds to judge"},
        # non-AI rows are regenerated from config at approval — filtered out
        {"section_id": "human_review_required", "score_type": "manual_binary",
         "numeric_score": None, "binary_value": "NA",
         "score_source": "manual_default", "confidence": None, "reasoning": None},
        {"section_id": "documentation", "score_type": "auto_value",
         "numeric_score": None, "binary_value": "Y",
         "score_source": "auto_value", "confidence": None, "reasoning": None},
    ]


class FakeConn:
    def __init__(self, row, sections):
        self.row = row
        self.sections = sections
        self.fetchrow_args = None

    async def fetchrow(self, query, *args):
        self.fetchrow_args = (query, args)
        return self.row

    async def fetch(self, query, *args):
        return self.sections


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


@pytest.mark.asyncio
class TestResolveEvaluation:

    @pytest.fixture(autouse=True)
    def _stub_pool(self, monkeypatch):
        self.conn = FakeConn(_eval_row(), _section_rows())

        async def fake_get_pool():
            return FakePool(self.conn)

        monkeypatch.setattr(eval_store, "_get_pool", fake_get_pool)

    async def test_resolves_by_job_id_and_probes_both_id_columns(self):
        ref = await resolve_evaluation("member_support", "5035229460504576_Juan_Celso")
        assert ref is not None
        assert ref.id == 2377
        assert ref.state == "draft"
        assert ref.scoring_status == "flagged_human_review"
        assert ref.model == "gemini-2.5-flash"
        # S4 fields — series-rebuild target and long-call-flag parity.
        assert ref.agent_id == 7
        assert ref.duration_ms == 180000.0
        assert isinstance(ref.duration_ms, float)
        # The 2026-07-24 incident guarantee: one probe, both columns.
        query, args = self.conn.fetchrow_args
        assert "dialpad_entry_point_call_id = $2" in query
        assert "dialpad_call_id = $2" in query
        assert args == ("member_support", "5035229460504576")

    async def test_sections_reshaped_to_draft_dump(self):
        ref = await resolve_evaluation("member_support", "5035229460504576")
        by_id = {s["id"]: s for s in ref.sections}
        # AI rows only — manual_default and auto_value rows are excluded.
        assert set(by_id) == {"greeting", "caller_id", "efficiency"}
        assert by_id["greeting"] == {
            "id": "greeting", "name": "greeting", "score": 5,
            "score_type": "numeric", "yn_value": None, "confidence": "high",
            "reasoning": "warm open", "audio_dependent": False, "flags": [],
        }
        assert by_id["caller_id"]["score_type"] == "yn"
        assert by_id["caller_id"]["score"] is None
        assert by_id["caller_id"]["yn_value"] == "Y"
        assert by_id["caller_id"]["confidence"] == "medium"
        # numeric NA keeps the (score=None, yn_value='NA') draft shape.
        assert by_id["efficiency"]["score"] is None
        assert by_id["efficiency"]["yn_value"] == "NA"
        assert by_id["efficiency"]["score_type"] == "numeric"

    async def test_models_used_accepts_dict_codec(self):
        # asyncpg with a JSONB codec hands back a dict, not a str.
        self.conn.row = _eval_row(models_used={"text": {"model": "gemma-4-27b-q4"}})
        ref = await resolve_evaluation("member_support", "5035229460504576")
        assert ref.model == "gemma-4-27b-q4"

    async def test_overall_score_numeric_to_float(self):
        self.conn.row = _eval_row(state="finalized", overall_score=85)
        ref = await resolve_evaluation("member_support", "5035229460504576")
        assert ref.overall_score == 85.0
        assert isinstance(ref.overall_score, float)

    async def test_miss_returns_none(self):
        self.conn.row = None
        assert await resolve_evaluation("member_support", "999") is None

    async def test_non_numeric_job_id_skips_probe(self):
        assert await resolve_evaluation("member_support", "Juan_Celso") is None
        assert self.conn.fetchrow_args is None

    async def test_no_pool_returns_none(self, monkeypatch):
        async def no_pool():
            return None
        monkeypatch.setattr(eval_store, "_get_pool", no_pool)
        assert await resolve_evaluation("member_support", "5035229460504576") is None


# ---------------------------------------------------------------------------
# S5 helpers — fetch_auto_rescore_state / stamp_auto_rescored /
# mark_human_review_required (ScorecardActionsDesign §4.2)
# ---------------------------------------------------------------------------

from backend.services.eval_store import (  # noqa: E402
    fetch_auto_rescore_state,
    mark_human_review_required,
    stamp_auto_rescored,
)


class _S5Conn:
    def __init__(self, row=None):
        self.row = row
        self.fetchrow_calls: list[tuple] = []
        self.execute_calls: list[tuple] = []

    async def fetchrow(self, query, *args):
        self.fetchrow_calls.append((query, args))
        return self.row

    async def execute(self, query, *args):
        self.execute_calls.append((query, args))
        return "UPDATE 1"


@pytest.mark.asyncio
class TestAutoRescoreHelpers:

    def _wire(self, monkeypatch, conn):
        async def fake_get_pool():
            return FakePool(conn)
        monkeypatch.setattr(eval_store, "_get_pool", fake_get_pool)

    async def test_fetch_state_maps_row(self, monkeypatch):
        conn = _S5Conn(row={
            "source": "ai", "auto_rescored_at": None, "agent_id": 7,
            "duration_ms": 180000, "dialpad_call_id": "503",
            "dialpad_entry_point_call_id": "610",
            "agent_name_raw": "Juan Celso", "agent_email": "j@landing.com",
            "models_used": json.dumps({"text": {"model": "claude-sonnet-5"}}),
        })
        self._wire(monkeypatch, conn)
        state = await fetch_auto_rescore_state(2377)
        assert state["source"] == "ai"
        assert state["auto_rescored_at"] is None
        assert state["duration_ms"] == 180000.0
        # The model stamp survives a provider flip — P3+ traceability.
        assert state["model"] == "claude-sonnet-5"
        assert conn.fetchrow_calls[0][1] == (2377,)

    async def test_fetch_state_missing_row_is_none(self, monkeypatch):
        conn = _S5Conn(row=None)
        self._wire(monkeypatch, conn)
        assert await fetch_auto_rescore_state(999) is None

    async def test_stamp_latch_wins(self, monkeypatch):
        conn = _S5Conn(row={"id": 2377})
        self._wire(monkeypatch, conn)
        assert await stamp_auto_rescored(2377) is True
        query, args = conn.fetchrow_calls[0]
        # The once-EVER guarantee is IN the SQL, not app logic.
        assert "auto_rescored_at IS NULL" in query
        assert "RETURNING id" in query
        assert args == (2377,)

    async def test_stamp_latch_already_stamped_returns_false(self, monkeypatch):
        conn = _S5Conn(row=None)
        self._wire(monkeypatch, conn)
        assert await stamp_auto_rescored(2377) is False

    async def test_mark_queue_preserves_existing_marker(self, monkeypatch):
        conn = _S5Conn()
        self._wire(monkeypatch, conn)
        await mark_human_review_required(2377)
        query, args = conn.execute_calls[0]
        assert "COALESCE(human_review_required_at, NOW())" in query
        assert args == (2377,)

    async def test_no_pool_degrades_quietly(self, monkeypatch):
        async def no_pool():
            return None
        monkeypatch.setattr(eval_store, "_get_pool", no_pool)
        assert await fetch_auto_rescore_state(1) is None
        assert await stamp_auto_rescored(1) is False
        await mark_human_review_required(1)  # no raise


# ---------------------------------------------------------------------------
# S6 — apply_override / coaching receipts / review queue
# (ScorecardActionsDesign §4.3, §4.3a, §0.3)
# ---------------------------------------------------------------------------

from types import SimpleNamespace  # noqa: E402

from backend.services.eval_store import (  # noqa: E402
    apply_override,
    complete_coaching_notified,
    create_notification_coaching,
    create_resolution_receipt,
    list_review_queue,
)


class _S6Conn:
    """Records queries in order; answers the coaching INSERT with an id."""

    def __init__(self, agent_row=None, queue_rows=()):
        self.agent_row = agent_row
        self.queue_rows = list(queue_rows)
        self.queries: list[tuple[str, tuple]] = []
        self.fail_on_rebuild = False

    @asynccontextmanager
    async def transaction(self):
        self.queries.append(("BEGIN", ()))
        yield
        self.queries.append(("COMMIT", ()))

    async def fetchrow(self, query, *args):
        self.queries.append((query, args))
        if "INSERT INTO qa.coachings" in query:
            return {"id": 55}
        if "SELECT agent_id" in query:
            return self.agent_row
        raise AssertionError(f"unexpected fetchrow: {query}")

    async def fetch(self, query, *args):
        self.queries.append((query, args))
        return self.queue_rows

    async def execute(self, query, *args):
        self.queries.append((query, args))
        return "UPDATE 1"


def _sql_ops(conn):
    return [q for q, _ in conn.queries if q not in ("BEGIN", "COMMIT")]


@pytest.mark.asyncio
class TestApplyOverride:

    def _wire(self, monkeypatch, conn, rebuild_calls=None):
        async def fake_get_pool():
            return FakePool(conn)
        monkeypatch.setattr(eval_store, "_get_pool", fake_get_pool)

        calls = rebuild_calls if rebuild_calls is not None else []

        async def fake_rebuild(c, agent_id, config):
            if conn.fail_on_rebuild:
                raise RuntimeError("rebuild boom")
            calls.append(agent_id)
            conn.queries.append(("REBUILD", (agent_id,)))
            return 3
        import backend.services.stat_points as sp
        monkeypatch.setattr(sp, "rebuild_agent_series", fake_rebuild)
        return calls

    async def _run(self):
        return await apply_override(
            evaluation_id=2377, team_id="member_support", agent_id=7,
            agent_name="Luis Rubio", old_score=62.5, new_score=85.0,
            reasoning="hold SOP v3", conducted_by_role="manager",
            evaluator_email="ana@landing.com",
            config=SimpleNamespace(),
        )

    async def test_supersede_touches_only_score_source_and_queue(self, monkeypatch):
        """Engine-score reproducibility: the UPDATE must leave sections
        and formula_version alone — old score reproducible forever."""
        conn = _S6Conn()
        self._wire(monkeypatch, conn)
        coaching_id = await self._run()
        assert coaching_id == 55

        update = next(q for q, _ in conn.queries if q.startswith("UPDATE qa.evaluations"))
        assert "overall_score = $2" in update
        assert "source = 'ai_reviewed'" in update
        assert "human_review_completed_at" in update  # queue exit (§0.3)
        assert "formula_version" not in update
        assert "evaluation_sections" not in update

    async def test_order_update_receipt_rebuild_one_transaction(self, monkeypatch):
        rebuilds = []
        conn = _S6Conn()
        self._wire(monkeypatch, conn, rebuilds)
        await self._run()
        ops = _sql_ops(conn)
        assert ops[0].startswith("UPDATE qa.evaluations")
        assert "INSERT INTO qa.coachings" in ops[1]
        assert "INSERT INTO qa.coaching_evaluations" in ops[2]
        assert ops[3] == "REBUILD"
        assert rebuilds == [7]
        # Everything between the outermost BEGIN/COMMIT pair.
        assert conn.queries[0][0] == "BEGIN"
        assert conn.queries[-1][0] == "COMMIT"

    async def test_receipt_carries_deadline_and_action_plan(self, monkeypatch):
        conn = _S6Conn()
        self._wire(monkeypatch, conn)
        await self._run()
        insert_q, insert_args = next(
            (q, a) for q, a in conn.queries if "INSERT INTO qa.coachings" in q)
        assert "'pending'" in insert_q
        assert "interval '3 days'" in insert_q
        assert insert_args[2] == "manager"
        assert insert_args[3] == "ana@landing.com"
        assert insert_args[4] == "Notify Luis Rubio: score manually overridden 62.5 → 85.0"
        link_q, link_args = next(
            (q, a) for q, a in conn.queries
            if "INSERT INTO qa.coaching_evaluations" in q)
        assert "opportunities FROM qa.evaluations" in link_q  # snapshot
        assert link_args == (55, 2377, "hold SOP v3")

    async def test_rebuild_failure_rolls_back(self, monkeypatch):
        """Fatal by contract (§5) — apply_override must propagate."""
        conn = _S6Conn()
        conn.fail_on_rebuild = True
        self._wire(monkeypatch, conn)
        with pytest.raises(RuntimeError, match="rebuild boom"):
            await self._run()


@pytest.mark.asyncio
class TestReceiptHelpers:

    def _wire(self, monkeypatch, conn):
        async def fake_get_pool():
            return FakePool(conn)
        monkeypatch.setattr(eval_store, "_get_pool", fake_get_pool)

    async def test_complete_guarded_by_pending_status(self, monkeypatch):
        conn = _S6Conn()
        self._wire(monkeypatch, conn)
        await complete_coaching_notified(
            55, completed_by="ana@landing.com", summary="notified")
        q, args = conn.queries[0]
        assert "status = 'completed'" in q
        assert "WHERE id = $1 AND status = 'pending'" in q
        assert args == (55, "notified", "ana@landing.com")

    async def test_resolution_receipt_skips_agentless(self, monkeypatch):
        conn = _S6Conn(agent_row={"agent_id": None})
        self._wire(monkeypatch, conn)
        assert await create_resolution_receipt(
            evaluation_id=5, team_id="member_support",
            evaluator_email="ana@landing.com", agent_name="Ghost") is None

    async def test_resolution_receipt_creates(self, monkeypatch):
        conn = _S6Conn(agent_row={"agent_id": 7})
        self._wire(monkeypatch, conn)
        cid = await create_resolution_receipt(
            evaluation_id=5, team_id="member_support",
            evaluator_email="ana@landing.com", agent_name="Luis Rubio")
        assert cid == 55
        insert_q, insert_args = next(
            (q, a) for q, a in conn.queries if "INSERT INTO qa.coachings" in q)
        assert insert_args[2] == "manager"
        assert "resolved by" in insert_args[4]


@pytest.mark.asyncio
class TestListReviewQueue:

    async def test_queue_filter_and_mapping(self, monkeypatch):
        conn = _S6Conn(queue_rows=[{
            "id": 2377, "dialpad_call_id": "503",
            "dialpad_entry_point_call_id": None,
            "agent_name_raw": "Luis Rubio", "agent_email": "l@landing.com",
            "overall_score": 45, "state": "finalized",
            "scoring_status": "complete", "source": "ai",
            "human_review_required_at": "t1", "auto_rescored_at": "t0",
            "finalized_at": "t1",
        }])

        async def fake_get_pool():
            return FakePool(conn)
        monkeypatch.setattr(eval_store, "_get_pool", fake_get_pool)

        rows = await list_review_queue("member_support")
        q, args = conn.queries[0]
        assert "human_review_required_at IS NOT NULL" in q
        assert "human_review_completed_at IS NULL" in q
        assert "ORDER BY human_review_required_at" in q
        assert args == ("member_support",)
        assert rows[0]["overall_score"] == 45.0
        assert isinstance(rows[0]["overall_score"], float)

    async def test_no_pool_returns_empty(self, monkeypatch):
        async def no_pool():
            return None
        monkeypatch.setattr(eval_store, "_get_pool", no_pool)
        assert await list_review_queue("member_support") == []

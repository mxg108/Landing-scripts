"""Stage 1 dual-write tests — Wave 2 Phase 4a.

The pure builders (scorecard → qa.evaluations dict + qa.evaluation_sections
models) carry most of the correctness weight; the upsert flow and the §7.3
swallow-everything contract are exercised against a stub pool/connection.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from datetime import datetime, timezone

import pytest

from backend.models.scorecard import ScorecardSection, ScorecardWithMeta
from backend.services import eval_store
from backend.services.eval_store import (
    build_approval_sections,
    build_draft_row,
    build_draft_sections,
    record_draft_evaluation,
)
from tests.conftest import load_test_config


@pytest.fixture(scope="module")
def ms_config():
    return load_test_config("member_support")


def _ai_section(sec_id, name, *, score=None, yn=None, score_type="numeric",
                confidence="high", reasoning="because", flags=None):
    return ScorecardSection(
        id=sec_id, name=name, score=score, yn_value=yn, score_type=score_type,
        confidence=confidence, reasoning=reasoning, flags=flags or [],
    )


def _scorecard(ms_config, **overrides):
    """A full MS draft: 8 AI-scored sections (manual + auto get no AI output)."""
    sections = [
        _ai_section("greeting", "Greeting", score=5),
        _ai_section("caller_id", "Caller ID", yn="Y", score_type="yn", confidence="medium"),
        _ai_section("purpose", "Purpose", score=4),
        _ai_section("matching", "Matching", score=4, confidence="low"),
        _ai_section("process_adherence", "Process", score=5),
        _ai_section("call_resolution", "Resolution", score=3),
        _ai_section("comms", "Communication", score=4),
        _ai_section("efficiency", "Efficiency", score=2, flags=["long_hold"]),
    ]
    defaults = dict(
        sections=sections,
        key_strengths="Good tone",
        opportunities="Faster holds",
        call_summary="Member called about billing",
        call_id="DP123",
        agent_name="Jane Agent",
        manager_email="lead@landing.com",
        dialpad_link="https://dialpad.test/call/DP123",
        duration_ms=354000.0,
        model="gemini-2.5-flash",
        sop_used="Billing SOP",
        caller_name="Pat Caller",
        caller_phone="+15550100",
        call_started_at_utc=datetime(2026, 7, 2, 15, 30, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    return ScorecardWithMeta(**defaults)


# ---------------------------------------------------------------------------
# build_draft_row
# ---------------------------------------------------------------------------

class TestBuildDraftRow:

    def test_stage1_row_shape(self, ms_config):
        row = build_draft_row(_scorecard(ms_config), ms_config)
        assert row["state"] == "draft"
        assert row["source"] == "ai"
        assert row["team_id"] == "member_support"
        assert row["dialpad_call_id"] == "DP123"
        assert row["call_duration_ms"] == 354000
        assert row["ai_provider_primary"] == "gemini"
        assert row["scoring_status"] == "complete"

    def test_versions_not_stamped_at_draft_time(self, ms_config):
        """§3.6 — formula/rubric versions are stamped at score-compute time
        (Stage 2 post-cutover), never at draft."""
        row = build_draft_row(_scorecard(ms_config), ms_config)
        assert "formula_version" not in row
        assert "rubric_version" not in row
        assert "overall_score" not in row

    def test_models_used_shape(self, ms_config):
        row = build_draft_row(_scorecard(ms_config), ms_config)
        assert json.loads(row["models_used"]) == {
            "text": {"provider": "gemini", "model": "gemini-2.5-flash"}
        }

    def test_flagged_long_call_sets_scoring_status(self, ms_config):
        row = build_draft_row(_scorecard(ms_config, flagged_long_call=True), ms_config)
        assert row["scoring_status"] == "flagged_long_call"

    def test_section_flags_collected_into_metadata(self, ms_config):
        meta = json.loads(build_draft_row(_scorecard(ms_config), ms_config)["dialpad_call_metadata"])
        assert meta["stage1_flags"] == ["long_hold"]
        assert meta["sop_used"] == "Billing SOP"


# ---------------------------------------------------------------------------
# build_draft_sections
# ---------------------------------------------------------------------------

class TestBuildDraftSections:

    def test_ms_draft_has_nine_rows(self, ms_config):
        """8 AI sections + human_review_required NA-default; MS has no
        auto_value sections; cri got no AI output in this fixture? — no:
        cri IS in the rubric but absent from the scorecard, so no row."""
        rows = build_draft_sections(_scorecard(ms_config), ms_config)
        by_id = {r.section_id: r for r in rows}
        assert len(rows) == 9
        assert "cri" not in by_id  # no AI output → no draft row

    def test_numeric_ai_section(self, ms_config):
        rows = build_draft_sections(_scorecard(ms_config), ms_config)
        greeting = next(r for r in rows if r.section_id == "greeting")
        assert greeting.score_type == "numeric"
        assert greeting.numeric_score == 5
        assert greeting.binary_value is None
        assert greeting.score_source == "ai"
        assert greeting.ai_provider == "gemini"
        assert greeting.model == "gemini-2.5-flash"
        assert greeting.confidence == "HIGH"

    def test_binary_ai_section_and_confidence_map(self, ms_config):
        rows = build_draft_sections(_scorecard(ms_config), ms_config)
        caller = next(r for r in rows if r.section_id == "caller_id")
        assert caller.score_type == "binary"
        assert caller.binary_value == "Y"
        assert caller.numeric_score is None
        assert caller.confidence == "MED"

    def test_manual_na_default_row(self, ms_config):
        """human_review_required — the §3.8-point-7 / migration-012 shape."""
        rows = build_draft_sections(_scorecard(ms_config), ms_config)
        hrr = next(r for r in rows if r.section_id == "human_review_required")
        assert hrr.score_type == "manual_numeric"
        assert hrr.binary_value == "NA"
        assert hrr.numeric_score is None
        assert hrr.score_source == "manual_default"
        assert hrr.ai_provider is None

    def test_binary_na_from_ai(self, ms_config):
        sc = _scorecard(ms_config)
        sc.sections[1] = _ai_section("caller_id", "Caller ID", yn="NA", score_type="yn")
        caller = next(
            r for r in build_draft_sections(sc, ms_config) if r.section_id == "caller_id"
        )
        assert caller.score_type == "binary"
        assert caller.binary_value == "NA"

    def test_numeric_na_from_ai_uses_migration_012_shape(self, ms_config):
        """An NA on a numeric na_applicable section (efficiency is not —
        use a matching-shaped override) lands as numeric + binary_value NA."""
        sc = _scorecard(ms_config)
        sc.sections[7] = ScorecardSection(
            id="efficiency", name="Efficiency", score=None, yn_value="NA",
            score_type="numeric", confidence="low", reasoning="silent call",
        )
        eff = next(
            r for r in build_draft_sections(sc, ms_config) if r.section_id == "efficiency"
        )
        assert eff.score_type == "numeric"
        assert eff.numeric_score is None
        assert eff.binary_value == "NA"
        assert eff.score_source == "ai"

    def test_auto_value_sections_write_auto_rows(self):
        """Sales Q18 screen_recording: auto_value='Yes' → binary Y row."""
        sales = load_test_config("sales")
        sc = ScorecardWithMeta(
            sections=[], key_strengths="", opportunities="",
            model="gemini-2.5-flash",
        )
        rows = build_draft_sections(sc, sales)
        auto = [r for r in rows if r.score_source == "auto_value"]
        assert auto, "sales config has an auto_value section"
        assert all(r.binary_value == "Y" for r in auto)


# ---------------------------------------------------------------------------
# record_draft_evaluation — upsert + §7.3 swallow
# ---------------------------------------------------------------------------

class FakeConn:
    def __init__(self, existing_id=None):
        self.existing_id = existing_id
        self.inserts: list[tuple] = []
        self.updates: list[tuple] = []
        self.deletes: list[tuple] = []

    @asynccontextmanager
    async def transaction(self):
        yield

    async def fetchval(self, query, *args):
        if query.startswith("SELECT id"):
            return self.existing_id
        assert query.startswith("INSERT INTO qa.evaluations")
        self.inserts.append(("evaluations", args))
        return 101

    async def execute(self, query, *args):
        if query.startswith("UPDATE"):
            self.updates.append(args)
        elif query.startswith("DELETE"):
            self.deletes.append(args)
        else:
            self.inserts.append(("sections", args))


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


class TestRecordDraftEvaluation:
    pytestmark = pytest.mark.asyncio

    @pytest.fixture(autouse=True)
    def _no_real_pool(self, monkeypatch):
        self.conn = FakeConn()

        async def fake_get_pool():
            return FakePool(self.conn)

        monkeypatch.setattr(eval_store, "_get_pool", fake_get_pool)

    async def test_new_draft_inserts_eval_and_sections(self, ms_config):
        evaluation_id = await record_draft_evaluation(_scorecard(ms_config), ms_config)
        assert evaluation_id == 101
        eval_inserts = [i for i in self.conn.inserts if i[0] == "evaluations"]
        section_inserts = [i for i in self.conn.inserts if i[0] == "sections"]
        assert len(eval_inserts) == 1
        assert len(section_inserts) == 9
        assert all(args[0] == 101 for _, args in section_inserts)

    async def test_rescore_updates_row_and_replaces_sections(self, ms_config):
        self.conn.existing_id = 77
        evaluation_id = await record_draft_evaluation(_scorecard(ms_config), ms_config)
        assert evaluation_id == 77
        assert len(self.conn.updates) == 1
        assert self.conn.deletes == [(77,)]
        assert not [i for i in self.conn.inserts if i[0] == "evaluations"]

    async def test_db_failure_is_swallowed(self, ms_config, monkeypatch):
        async def exploding_pool():
            raise ConnectionError("pg down")

        monkeypatch.setattr(eval_store, "_get_pool", exploding_pool)
        assert await record_draft_evaluation(_scorecard(ms_config), ms_config) is None

    async def test_mid_write_failure_is_swallowed(self, ms_config, monkeypatch):
        async def boom(*a, **k):
            raise RuntimeError("constraint violation")

        self.conn.fetchval = boom
        assert await record_draft_evaluation(_scorecard(ms_config), ms_config) is None


class TestPoolDisabled:
    pytestmark = pytest.mark.asyncio

    async def test_no_database_url_returns_none(self, ms_config, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.setattr(eval_store, "_pool", None)
        assert await record_draft_evaluation(_scorecard(ms_config), ms_config) is None


# ---------------------------------------------------------------------------
# record_approval — Stages 1.5+2+3+4 (Phase 4b)
# ---------------------------------------------------------------------------

def _approval_sections(ms_config, **edits):
    """Approval payload mirroring the Stage-1 draft, with optional edits."""
    base = {
        "greeting": _ai_section("greeting", "Greeting", score=5),
        "caller_id": _ai_section("caller_id", "Caller ID", yn="Y", score_type="yn", confidence="medium"),
        "purpose": _ai_section("purpose", "Purpose", score=4),
        "matching": _ai_section("matching", "Matching", score=4, confidence="low"),
        "process_adherence": _ai_section("process_adherence", "Process", score=5),
        "call_resolution": _ai_section("call_resolution", "Resolution", score=3),
        "comms": _ai_section("comms", "Communication", score=4),
        "efficiency": _ai_section("efficiency", "Efficiency", score=2),
        "human_review_required": ScorecardSection(
            id="human_review_required", name="HRR", score=None, yn_value="NA",
            score_type="manual", confidence="high", reasoning="",
        ),
        # cri deliberately absent — the Stage-1 draft fixture has no AI
        # output for it, and the payload mirrors the draft.
    }
    base.update(edits)
    return list(base.values())


class TestBuildApprovalSections:

    def test_unchanged_sections_keep_ai_provenance(self, ms_config):
        draft = [s.model_dump() for s in _scorecard(ms_config).sections]
        rows, changed = build_approval_sections(
            _approval_sections(ms_config), draft, ms_config, "gemini-2.5-flash"
        )
        assert not changed
        greeting = next(r for r in rows if r.section_id == "greeting")
        assert greeting.score_source == "ai"
        assert greeting.ai_provider == "gemini"

    def test_edited_section_becomes_manual_without_ai_fields(self, ms_config):
        draft = [s.model_dump() for s in _scorecard(ms_config).sections]
        rows, changed = build_approval_sections(
            _approval_sections(ms_config, comms=_ai_section("comms", "Communication", score=2)),
            draft, ms_config, "gemini-2.5-flash",
        )
        assert changed
        comms = next(r for r in rows if r.section_id == "comms")
        assert comms.score_source == "manual"
        assert comms.numeric_score == 2
        assert comms.ai_provider is None
        assert comms.confidence is None
        # only the edited section flips; the rest keep AI provenance
        assert next(r for r in rows if r.section_id == "purpose").score_source == "ai"

    def test_untouched_hrr_keeps_manual_default_na(self, ms_config):
        draft = [s.model_dump() for s in _scorecard(ms_config).sections]
        rows, _ = build_approval_sections(
            _approval_sections(ms_config), draft, ms_config, "gemini-2.5-flash"
        )
        hrr = next(r for r in rows if r.section_id == "human_review_required")
        assert hrr.score_source == "manual_default"
        assert hrr.binary_value == "NA"

    def test_value_without_ai_draft_counts_as_analyst_edit(self, ms_config):
        """A section that appears at approval without a Stage-1 AI value was
        supplied by the analyst — manual provenance, source flips."""
        draft = [s.model_dump() for s in _scorecard(ms_config).sections]  # no cri
        rows, changed = build_approval_sections(
            _approval_sections(ms_config) + [_ai_section("cri", "CRI", yn="Y", score_type="yn")],
            draft, ms_config, "gemini-2.5-flash",
        )
        assert changed
        cri = next(r for r in rows if r.section_id == "cri")
        assert cri.score_source == "manual"
        assert cri.ai_provider is None

    def test_supervisor_scored_hrr_becomes_manual(self, ms_config):
        draft = [s.model_dump() for s in _scorecard(ms_config).sections]
        rows, changed = build_approval_sections(
            _approval_sections(ms_config, human_review_required=ScorecardSection(
                id="human_review_required", name="HRR", score=1, yn_value=None,
                score_type="manual", confidence="high", reasoning="agreed with flag",
            )),
            draft, ms_config, "gemini-2.5-flash",
        )
        assert changed
        hrr = next(r for r in rows if r.section_id == "human_review_required")
        assert hrr.score_type == "manual_numeric"
        assert hrr.numeric_score == 1
        assert hrr.score_source == "manual"


class ApprovalFakeConn(FakeConn):
    def __init__(self, existing_id=None):
        super().__init__(existing_id)
        self.eval_updates: list[tuple] = []
        self.eval_update_queries: list[str] = []

    async def execute(self, query, *args):
        if query.startswith("UPDATE qa.evaluations"):
            self.eval_updates.append(args)
            self.eval_update_queries.append(query)
        elif query.startswith("DELETE"):
            self.deletes.append(args)
        else:
            self.inserts.append(("sections", args))


class TestRecordApproval:
    pytestmark = pytest.mark.asyncio

    @pytest.fixture(autouse=True)
    def _no_real_pool(self, monkeypatch):
        self.conn = ApprovalFakeConn()

        async def fake_get_pool():
            return FakePool(self.conn)

        monkeypatch.setattr(eval_store, "_get_pool", fake_get_pool)

    async def _approve(self, ms_config, overall="87", evaluation_id=101, **kwargs):
        draft = [s.model_dump() for s in _scorecard(ms_config).sections]
        return await eval_store.record_approval(
            ms_config,
            evaluation_id=evaluation_id,
            dialpad_link="https://dialpad.test/call/DP123",
            evaluator_email="lead@landing.com",
            approved_sections=kwargs.pop("sections", _approval_sections(ms_config)),
            draft_sections=draft,
            key_strengths="Good tone",
            opportunities="Faster holds",
            overall_score_raw=overall,
            **kwargs,
        )

    async def test_finalizes_with_readback_score(self, ms_config):
        assert await self._approve(ms_config) == 101
        (args,) = self.conn.eval_updates
        # (id, evaluator, agent_email, ks, opp, overall, source, state)
        assert args[0] == 101
        assert args[1] == "lead@landing.com"
        assert args[5] == 87.0
        assert args[6] == "ai"          # nothing edited
        assert args[7] == "finalized"
        assert self.conn.deletes == [(101,)]
        # 8 AI sections + human_review_required (cri absent from payload)
        assert len([i for i in self.conn.inserts if i[0] == "sections"]) == 9

    async def test_blank_readback_lands_as_approved_not_finalized(self, ms_config):
        """§3.4.3: finalized requires overall_score — a failed ARRAYFORMULA
        readback leaves the row 'approved' with NULL score."""
        await self._approve(ms_config, overall="")
        (args,) = self.conn.eval_updates
        assert args[5] is None
        assert args[7] == "approved"

    async def test_edited_sections_flip_source_to_ai_reviewed(self, ms_config):
        await self._approve(
            ms_config,
            sections=_approval_sections(
                ms_config, comms=_ai_section("comms", "Communication", score=2)
            ),
        )
        (args,) = self.conn.eval_updates
        assert args[6] == "ai_reviewed"

    async def test_missing_draft_row_is_skipped(self, ms_config):
        self.conn.existing_id = None
        assert await self._approve(ms_config, evaluation_id=None) is None
        assert self.conn.eval_updates == []

    async def test_db_failure_is_swallowed(self, ms_config, monkeypatch):
        async def exploding_pool():
            raise ConnectionError("pg down")

        monkeypatch.setattr(eval_store, "_get_pool", exploding_pool)
        assert await self._approve(ms_config) is None


# ---------------------------------------------------------------------------
# §3.14 human-review trigger + shadow logging (cutover slice 1)
# ---------------------------------------------------------------------------

V4_FULL_RULES = (Path(__file__).resolve().parent.parent / "backend" / "config"
                 / "scoring" / "member_support" / "v4_full_rules.json")


def _v4_formula():
    from backend.models.formula import Formula
    return Formula.model_validate(json.loads(V4_FULL_RULES.read_text(encoding="utf-8")))


class TestHumanReviewTrigger:
    """Trigger MECHANICS against the archived v4 full-rule formula; the
    shipped July v5 has human_review_triggers=[] (gate off, Director
    2026-07-06) — asserted below."""

    @pytest.fixture(scope="class")
    def ms_formula(self):
        return _v4_formula()

    def test_shipped_v5_gate_is_off(self):
        active = eval_store._active_formula("member_support")
        assert active.formula_id == "member_support_v5"
        assert active.human_review_triggers == []
        assert not eval_store.human_review_trigger_fired(
            active, _scorecard(None, sections=[
                _ai_section("process_adherence", "Process", score=1),
            ]).sections
        )

    def test_v3_thresholds(self, ms_formula):
        """member_support_v3: process/resolution rating <= 2 fires; 3 does
        not (tightened at the sign-off close)."""
        base = _scorecard(None)
        assert not eval_store.human_review_trigger_fired(ms_formula, base.sections)  # process 5 / resolution 3
        low = _scorecard(None, sections=[
            _ai_section("process_adherence", "Process", score=2),
        ])
        assert eval_store.human_review_trigger_fired(ms_formula, low.sections)
        exactly_three = _scorecard(None, sections=[
            _ai_section("process_adherence", "Process", score=3),
            _ai_section("call_resolution", "Resolution", score=3),
        ])
        assert not eval_store.human_review_trigger_fired(ms_formula, exactly_three.sections)

    def test_no_formula_never_fires(self):
        assert eval_store._active_formula("sales") is None  # pre-sales_v2
        assert not eval_store.human_review_trigger_fired(None, _scorecard(None).sections)

    def test_non_numeric_sections_never_trigger(self, ms_formula):
        na_only = _scorecard(None, sections=[
            _ai_section("process_adherence", "Process", score=None, yn=None,
                        score_type="numeric"),
        ])
        assert not eval_store.human_review_trigger_fired(ms_formula, na_only.sections)


class TestDraftFlagging:
    """Mechanics under the v4 full-rule formula (monkeypatched in) — the
    shipped July v5 never flags, which TestHumanReviewTrigger asserts."""

    @pytest.fixture(autouse=True)
    def _v4_policy(self, monkeypatch):
        formula = _v4_formula()
        monkeypatch.setattr(eval_store, "_active_formula", lambda team_id: formula)

    def test_flagged_draft_row(self, ms_config):
        sc = _scorecard(ms_config)
        sc.sections[4] = _ai_section("process_adherence", "Process", score=1)
        row = build_draft_row(sc, ms_config)
        assert row["scoring_status"] == "flagged_human_review"
        assert row["human_review_required_at"] is not None

    def test_human_review_beats_long_call_flag(self, ms_config):
        sc = _scorecard(ms_config, flagged_long_call=True)
        sc.sections[5] = _ai_section("call_resolution", "Resolution", score=2)
        row = build_draft_row(sc, ms_config)
        assert row["scoring_status"] == "flagged_human_review"

    def test_clean_draft_unchanged(self, ms_config):
        row = build_draft_row(_scorecard(ms_config), ms_config)
        assert row["scoring_status"] == "complete"
        assert row["human_review_required_at"] is None


class TestApprovalRecomputeAndShadow:
    pytestmark = pytest.mark.asyncio

    @pytest.fixture(autouse=True)
    def _no_real_pool(self, monkeypatch):
        self.conn = ApprovalFakeConn()

        async def fake_get_pool():
            return FakePool(self.conn)

        monkeypatch.setattr(eval_store, "_get_pool", fake_get_pool)
        # Recompute/shadow MECHANICS run against the archived v4 full-rule
        # formula; the shipped v5 gate-off behavior is covered separately.
        formula = _v4_formula()
        monkeypatch.setattr(eval_store, "_active_formula", lambda team_id: formula)

    async def _approve(self, ms_config, sections):
        draft = [s.model_dump() for s in _scorecard(ms_config).sections]
        return await eval_store.record_approval(
            ms_config, evaluation_id=101, dialpad_link=None,
            evaluator_email="lead@landing.com", approved_sections=sections,
            draft_sections=draft, key_strengths="", opportunities="",
            overall_score_raw="87",
        )

    async def test_analyst_lowering_score_fires_flag(self, ms_config):
        """§3.14: the trigger is recomputed at approval, not just Stage 1."""
        sections = _approval_sections(
            ms_config,
            process_adherence=_ai_section("process_adherence", "Process", score=2),
        )
        await self._approve(ms_config, sections)
        (args,) = self.conn.eval_updates
        assert args[8] == "flagged_human_review"
        assert args[9] is True      # flagged -> required_at kept/set (SQL CASE)

    async def test_clean_approval_clears_flag(self, ms_config):
        await self._approve(ms_config, _approval_sections(ms_config))
        (args,) = self.conn.eval_updates
        assert args[8] == "complete"
        assert args[9] is False
        assert args[10] is False    # not a resolution -> completed_at untouched

    async def test_shadow_log_emitted(self, ms_config, caplog):
        import logging
        full_payload = _approval_sections(ms_config) + [
            _ai_section("cri", "CRI", yn="Y", score_type="yn"),
        ]
        with caplog.at_level(logging.INFO, logger="backend.services.eval_store"):
            await self._approve(ms_config, full_payload)
        shadow = [m for m in (r.getMessage() for r in caplog.records)
                  if m.startswith("shadow: team=member_support")]
        assert len(shadow) == 1
        assert "engine=" in shadow[0] and "sheet=87" in shadow[0]
        assert "formula=member_support_v4" in shadow[0]  # v4 monkeypatched in
        assert "trigger_fired=False" in shadow[0]

    async def test_shadow_skips_incomplete_payload(self, ms_config, caplog):
        """A payload missing a formula section (cri here) must abort the
        shadow quietly — the engine's strict validation is not a user error."""
        import logging
        with caplog.at_level(logging.INFO, logger="backend.services.eval_store"):
            await self._approve(ms_config, _approval_sections(ms_config))
        assert not [r for r in caplog.records if r.getMessage().startswith("shadow: team=")]


# ---------------------------------------------------------------------------
# scoring_owner flag + strict mode + stamp_and_finalize (cutover slice 2)
# ---------------------------------------------------------------------------

class OwnerFakePool:
    def __init__(self, owner):
        self.owner = owner

    async def fetchval(self, query, *args):
        assert "scoring_owner FROM public.teams" in query
        return self.owner


class TestScoringOwner:
    pytestmark = pytest.mark.asyncio

    async def test_no_pool_defaults_to_sheets(self, monkeypatch):
        async def no_pool():
            return None
        monkeypatch.setattr(eval_store, "_get_pool", no_pool)
        assert await eval_store.get_scoring_owner("member_support") == "sheets"

    async def test_reads_flag(self, monkeypatch):
        async def pool():
            return OwnerFakePool("postgres")
        monkeypatch.setattr(eval_store, "_get_pool", pool)
        assert await eval_store.get_scoring_owner("member_support") == "postgres"

    async def test_lookup_failure_degrades_to_sheets(self, monkeypatch):
        async def exploding():
            raise ConnectionError("pg down")
        monkeypatch.setattr(eval_store, "_get_pool", exploding)
        assert await eval_store.get_scoring_owner("member_support") == "sheets"


class TestStrictMode:
    pytestmark = pytest.mark.asyncio

    async def test_draft_strict_raises_without_dsn(self, ms_config, monkeypatch):
        async def no_pool():
            return None
        monkeypatch.setattr(eval_store, "_get_pool", no_pool)
        with pytest.raises(RuntimeError, match="no DATABASE_URL"):
            await record_draft_evaluation(_scorecard(ms_config), ms_config, strict=True)

    async def test_draft_strict_reraises_db_errors(self, ms_config, monkeypatch):
        async def exploding():
            raise ConnectionError("pg down")
        monkeypatch.setattr(eval_store, "_get_pool", exploding)
        with pytest.raises(ConnectionError):
            await record_draft_evaluation(_scorecard(ms_config), ms_config, strict=True)

    async def test_approval_strict_reraises(self, ms_config, monkeypatch):
        async def exploding():
            raise ConnectionError("pg down")
        monkeypatch.setattr(eval_store, "_get_pool", exploding)
        with pytest.raises(ConnectionError):
            await eval_store.record_approval(
                ms_config, evaluation_id=1, dialpad_link=None,
                evaluator_email="x@y.com", approved_sections=[],
                draft_sections=[], key_strengths="", opportunities="",
                overall_score_raw=None, strict=True,
            )


class TestResolvingReview:
    pytestmark = pytest.mark.asyncio

    @pytest.fixture(autouse=True)
    def _no_real_pool(self, monkeypatch):
        self.conn = ApprovalFakeConn()

        async def fake_get_pool():
            return FakePool(self.conn)

        monkeypatch.setattr(eval_store, "_get_pool", fake_get_pool)

    async def test_reviewer_resolution_clears_flag_despite_low_scores(self, ms_config):
        """§3.14 step 4: the reviewer's judgment IS the resolution — the
        flag clears and completed_at stamps even with process still at 2."""
        sections = _approval_sections(
            ms_config,
            process_adherence=_ai_section("process_adherence", "Process", score=2),
        )
        draft = [s.model_dump() for s in _scorecard(ms_config).sections]
        await eval_store.record_approval(
            ms_config, evaluation_id=101, dialpad_link=None,
            evaluator_email="lead@landing.com", approved_sections=sections,
            draft_sections=draft, key_strengths="", opportunities="",
            overall_score_raw=None, resolving_review=True,
        )
        (args,) = self.conn.eval_updates
        assert args[8] == "complete"          # status back to complete
        assert args[9] is False               # NOT the recompute-fire branch
        assert args[10] is True               # resolution branch
        # Regression (production 2026-07-05, evaluations_human_review_pair_check):
        # the SQL must PRESERVE required_at on resolution — completed_at
        # without required_at violates the 009 pair CHECK.
        query = self.conn.eval_update_queries[0]
        assert "WHEN $11::boolean THEN human_review_required_at" in query
        assert "WHEN $11::boolean THEN NOW() ELSE human_review_completed_at" in query


class FinalizeFakeConn:
    def __init__(self):
        self.executed: list[tuple] = []
        self.queries: list[str] = []

    @asynccontextmanager
    async def transaction(self):
        yield

    async def execute(self, query, *args):
        assert query.startswith("UPDATE qa.evaluations")
        self.executed.append(args)
        self.queries.append(query)


class FinalizePool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


class TestStampAndFinalize:
    pytestmark = pytest.mark.asyncio

    async def test_stamps_versions_and_engine_score(self, ms_config, monkeypatch):
        from decimal import Decimal
        from backend.services import score_compute

        conn = FinalizeFakeConn()

        async def pool():
            return FinalizePool(conn)

        async def fake_versions(c, team_id):
            assert team_id == "member_support"
            return score_compute.ActiveVersions(
                formula_version="member_support_v3", rubric_version="member_support_v2"
            )

        class FakeDetail:
            overall_score = Decimal("91.7")

        async def fake_detail(c, evaluation_id, formula_version=None, rubric_version=None, signals=None):
            assert evaluation_id == 55
            assert formula_version == "member_support_v3"
            return FakeDetail()

        monkeypatch.setattr(eval_store, "_get_pool", pool)
        monkeypatch.setattr(score_compute, "get_active_versions", fake_versions)
        monkeypatch.setattr(score_compute, "compute_score_detail", fake_detail)

        detail = await eval_store.stamp_and_finalize(55, ms_config, "op@landing.com")
        assert detail.overall_score == Decimal("91.7")
        # Two statements, one transaction: qa.agents identity resolution
        # (slice 3.5 — fills agent_email for the GAS email recipient), then
        # the finalize UPDATE.
        resolve_args, args = conn.executed
        assert resolve_args == (55,)
        assert "FROM qa.agents" in conn.queries[0]
        assert "agent_email = COALESCE(e.agent_email, a.email)" in conn.queries[0]
        # (id, overall, formula_version, rubric_version, evaluator)
        assert args[0] == 55
        assert args[1] == Decimal("91.7")
        assert args[2] == "member_support_v3"
        assert args[3] == "member_support_v2"
        assert args[4] == "op@landing.com"

    async def test_raises_without_dsn(self, ms_config, monkeypatch):
        async def no_pool():
            return None
        monkeypatch.setattr(eval_store, "_get_pool", no_pool)
        with pytest.raises(RuntimeError, match="no DATABASE_URL"):
            await eval_store.stamp_and_finalize(1, ms_config, "x@y.com")

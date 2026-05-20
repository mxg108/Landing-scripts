"""Score_Audit helpers — pure row-builder unit tests + 1 gated live append.

The pure tests cover the row shape, the column order, action/role
validation, and the empty-result-row sentinel. The live test (skipped
unless ``AI_SCORING_LIVE_SHEETS=1``) appends a sentinel row to the real
Score_Audit tab and verifies the gspread response parses to a positive
row number.
"""

from __future__ import annotations

import os
import uuid

import pytest

from backend.config import score_audit as audit_cfg
from backend.services.sheets_service import _build_score_audit_row


# ---------------------------------------------------------------------------
# COLUMNS / constants
# ---------------------------------------------------------------------------

def test_columns_are_ten_in_canonical_order():
    """Schema lock — design pins A..J and PR 3 will reference the order."""
    assert audit_cfg.COLUMNS == [
        "timestamp", "api_key_role", "evaluator_email", "agent_email",
        "agent_name", "call_id", "target_team", "action", "result_row", "notes",
    ]


def test_actions_set_matches_design():
    assert audit_cfg.ACTIONS == frozenset({"scored", "denied", "approved"})


def test_roles_set_matches_key_identity():
    """Audit roles must stay in sync with KeyIdentity.role values."""
    assert audit_cfg.ROLES == frozenset({"team", "privileged"})


def test_host_team_is_member_support():
    """Single-source audit lives on member_support per design line 195-202."""
    assert audit_cfg.HOST_TEAM_ID == "member_support"


# ---------------------------------------------------------------------------
# _build_score_audit_row
# ---------------------------------------------------------------------------

def _kwargs(**overrides):
    base = dict(
        timestamp="2026-05-18T19:30:00Z",
        api_key_role="team",
        evaluator_email="ana@landing.com",
        agent_email="luis@landing.com",
        agent_name="Luis Rubio",
        call_id="abc123",
        target_team="member_support",
        action="scored",
        result_row=42,
        notes="",
    )
    base.update(overrides)
    return base


def test_build_row_writes_columns_in_order():
    row = _build_score_audit_row(**_kwargs())
    assert row == [
        "2026-05-18T19:30:00Z",
        "team",
        "ana@landing.com",
        "luis@landing.com",
        "Luis Rubio",
        "abc123",
        "member_support",
        "scored",
        "42",
        "",
    ]


def test_build_row_result_row_none_writes_blank():
    """Denials don't have a result row — empty string, not '0' or 'None'."""
    row = _build_score_audit_row(**_kwargs(result_row=None, action="denied"))
    assert row[8] == ""


def test_build_row_unknown_action_rejected():
    with pytest.raises(ValueError, match="unknown audit action"):
        _build_score_audit_row(**_kwargs(action="rejected"))


def test_build_row_unknown_role_rejected():
    with pytest.raises(ValueError, match="unknown api_key_role"):
        _build_score_audit_row(**_kwargs(api_key_role="admin"))


def test_build_row_privileged_role_accepted():
    row = _build_score_audit_row(**_kwargs(api_key_role="privileged"))
    assert row[1] == "privileged"


def test_build_row_approved_action_accepted():
    """Stage 4 finalize writes action='approved'."""
    row = _build_score_audit_row(**_kwargs(action="approved"))
    assert row[7] == "approved"


def test_build_row_notes_passthrough():
    row = _build_score_audit_row(**_kwargs(notes="no_recording"))
    assert row[9] == "no_recording"


# ---------------------------------------------------------------------------
# Live-Sheets smoke (skipped by default)
# ---------------------------------------------------------------------------

LIVE = os.environ.get("AI_SCORING_LIVE_SHEETS") == "1"


@pytest.mark.skipif(not LIVE, reason="set AI_SCORING_LIVE_SHEETS=1 to hit real Sheets")
def test_append_score_audit_row_live_smoke():
    """End-to-end append against the real Score_Audit tab.

    Writes a sentinel row tagged with a UUID in `notes` so it's easy to
    find + delete manually. Asserts only that the gspread response
    parses to a positive row number.
    """
    from backend.services.sheets_service import append_score_audit_row

    sentinel = f"smoke-{uuid.uuid4()}"
    row_num = append_score_audit_row(
        api_key_role="privileged",
        evaluator_email="smoke@landing.com",
        agent_email="smoke@landing.com",
        agent_name="Smoke Test",
        call_id="smoke-call-id",
        target_team="member_support",
        action="denied",
        result_row=None,
        notes=sentinel,
    )
    assert row_num > 0, f"append failed, got row_num={row_num}"

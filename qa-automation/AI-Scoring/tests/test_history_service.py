"""Mails-roster helpers — pure-function unit tests + 1 gated live-Sheets smoke.

The pure helpers (``_email_in_mails_rows``) work on synthetic
``list[list[str]]`` rows produced by ``conftest.make_mails_sheet`` — same
fixture pattern the team_stats tests use.

The async wrappers (``email_in_team_mails``, ``resolve_team_for_agent``)
hit real gspread. They are exercised by the smoke test at the bottom of
this module, which is skipped unless ``AI_SCORING_LIVE_SHEETS=1`` is set
in the environment (so CI without service-account creds stays green).
"""

from __future__ import annotations

import asyncio
import os

import pytest

from backend.services.history_service import (
    _agent_name_for_email_in_rows,
    _email_in_mails_rows,
)
from tests.conftest import make_mails_sheet


# ---------------------------------------------------------------------------
# Pure helper: _email_in_mails_rows
# ---------------------------------------------------------------------------

def test_email_in_mails_rows_hit():
    rows = make_mails_sheet(["Star Rep", "Decline Rep"])
    assert _email_in_mails_rows("star.rep@landing.com", rows) is True


def test_email_in_mails_rows_miss():
    rows = make_mails_sheet(["Star Rep"])
    assert _email_in_mails_rows("nobody@landing.com", rows) is False


def test_email_in_mails_rows_case_insensitive():
    rows = make_mails_sheet(["Star Rep"])
    assert _email_in_mails_rows("STAR.REP@LANDING.COM", rows) is True


def test_email_in_mails_rows_whitespace_tolerant():
    rows = make_mails_sheet(["Star Rep"])
    assert _email_in_mails_rows("  star.rep@landing.com  ", rows) is True


def test_email_in_mails_rows_empty_string_false():
    rows = make_mails_sheet(["Star Rep"])
    assert _email_in_mails_rows("", rows) is False


def test_email_in_mails_rows_empty_roster_false():
    rows = [["Agent Name", "Email", "Supervisor", "Canonical Name"]]
    assert _email_in_mails_rows("star.rep@landing.com", rows) is False


def test_email_in_mails_rows_skips_header():
    rows = [
        ["Agent Name", "star.rep@landing.com", "Supervisor", "Canonical Name"],
    ]
    assert _email_in_mails_rows("star.rep@landing.com", rows) is False


def test_email_in_mails_rows_handles_short_rows():
    rows = [
        ["Agent Name", "Email", "Supervisor"],
        ["No-Email Rep"],  # name-only row, no col B
        ["Star Rep", "star.rep@landing.com"],
    ]
    assert _email_in_mails_rows("star.rep@landing.com", rows) is True


# ---------------------------------------------------------------------------
# Pure helper: _agent_name_for_email_in_rows
# ---------------------------------------------------------------------------

def test_agent_name_for_email_hit():
    rows = make_mails_sheet(["Star Rep", "Decline Rep"])
    assert _agent_name_for_email_in_rows("star.rep@landing.com", rows) == "Star Rep"


def test_agent_name_for_email_miss_returns_none():
    rows = make_mails_sheet(["Star Rep"])
    assert _agent_name_for_email_in_rows("nobody@landing.com", rows) is None


def test_agent_name_for_email_case_insensitive():
    rows = make_mails_sheet(["Star Rep"])
    assert _agent_name_for_email_in_rows("STAR.REP@LANDING.COM", rows) == "Star Rep"


def test_agent_name_for_email_prefers_canonical_when_present():
    """Col D (Canonical Name) wins over col A when set."""
    rows = [
        ["Agent Name", "Email", "Supervisor", "Canonical Name"],
        ["luis", "luis@landing.com", "Sup A", "Luis Rubio"],
    ]
    assert _agent_name_for_email_in_rows("luis@landing.com", rows) == "Luis Rubio"


def test_agent_name_for_email_empty_string_returns_none():
    rows = make_mails_sheet(["Star Rep"])
    assert _agent_name_for_email_in_rows("", rows) is None


# ---------------------------------------------------------------------------
# Live-Sheets smoke (skipped by default)
# ---------------------------------------------------------------------------

LIVE = os.environ.get("AI_SCORING_LIVE_SHEETS") == "1"


@pytest.mark.skipif(not LIVE, reason="set AI_SCORING_LIVE_SHEETS=1 to hit real Sheets")
def test_email_in_team_mails_live_smoke():
    """Round-trip the cached SheetsProvider against the real Mails tab.

    Asserts only structural facts so the test stays stable as the roster
    changes — fetch succeeds, returns a bool, and a junk email is not a
    member.
    """
    from backend.services.history_service import email_in_team_mails

    result = asyncio.run(email_in_team_mails(
        "this-is-not-a-real-agent-xyz@landing.com", "member_support"
    ))
    assert result is False


@pytest.mark.skipif(not LIVE, reason="set AI_SCORING_LIVE_SHEETS=1 to hit real Sheets")
def test_resolve_team_for_agent_live_smoke():
    """Junk email resolves to None across all configured teams."""
    from backend.services.history_service import resolve_team_for_agent

    result = asyncio.run(
        resolve_team_for_agent("this-is-not-a-real-agent-xyz@landing.com")
    )
    assert result is None

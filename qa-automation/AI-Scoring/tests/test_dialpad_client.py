"""Unit tests for dialpad_client pure helpers.

Most of dialpad_client is async + HTTP-bound; this file only covers the
pure helpers that don't touch the network. Live-API smokes live under
scripts/.
"""

from __future__ import annotations

from backend.services.dialpad_client import build_dialpad_link


# ---------------------------------------------------------------------------
# build_dialpad_link — entry_point_call_id precedence
# ---------------------------------------------------------------------------

def test_build_dialpad_link_prefers_entry_point_id():
    """Dialpad's recording page is keyed by entry_point_call_id. When the
    caller supplies it, the link must use it, not the per-leg call_id."""
    link = build_dialpad_link(
        call_id="leg-123",
        entry_point_call_id="entry-999",
    )
    assert link == "https://dialpad.com/callhistory/callreview/entry-999"


def test_build_dialpad_link_falls_back_to_call_id():
    """Direct calls (and callers that haven't been updated) pass no
    entry_point_call_id; the link uses call_id directly."""
    link = build_dialpad_link("leg-123")
    assert link == "https://dialpad.com/callhistory/callreview/leg-123"


def test_build_dialpad_link_treats_empty_entry_point_as_absent():
    """An empty/whitespace entry_point_call_id should NOT produce a
    /callreview/ URL (the trailing path would be empty/whitespace and
    404 in Dialpad). Fall back to call_id instead."""
    link = build_dialpad_link("leg-123", entry_point_call_id="")
    assert link == "https://dialpad.com/callhistory/callreview/leg-123"
    link = build_dialpad_link("leg-123", entry_point_call_id="   ")
    assert link == "https://dialpad.com/callhistory/callreview/leg-123"


def test_build_dialpad_link_default_entry_point_is_empty():
    """Default-arg behavior unchanged from pre-refactor for existing
    callers that pass only call_id."""
    link = build_dialpad_link("leg-123")
    assert "leg-123" in link
    assert "callreview" in link

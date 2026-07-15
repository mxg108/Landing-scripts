"""Unit tests for the W1 Postgres row-source (ReadPathFlip §3/§4).

Covers the pure assembly functions — no live Postgres. The golden-parity
harness (scripts/parity_readpath.py) proves byte-identical compute_*
output against the real sheet; these tests pin the individual parity
traps in isolation so a regression names itself:

- eval_id parse + fallback chain (link text → superseded metadata link →
  entry-point call id);
- schema parity: frame_from_rows emits EXACTLY load_and_clean's columns;
- departed-agent LEFT-JOIN degradation (inactive, blank supervisor,
  raw name);
- excluded_test_agents filter, pre-2020 / null-timestamp row drops;
- numeric-NA → NaN, Y/N vocab, missing-section defaults;
- positional section-alias resolution across archived rubrics.
"""

from __future__ import annotations

import asyncio
import json
import math
from datetime import datetime

import pytest

from backend.services.team_source import (
    _eval_id_from_link,
    _section_alias_map,
    frame_from_rows,
)
from backend.services.team_stats import load_and_clean

from tests.conftest import make_history_sheet, make_mails_sheet


# ---------------------------------------------------------------------------
# Synthetic DB-row builders (asyncpg Records are dict-indexable; dicts stand in)
# ---------------------------------------------------------------------------

def _eval_row(**over) -> dict:
    row = {
        "id": 1,
        "agent_name_raw": "Star Rep",
        "agent_display": "Star Rep",
        "evaluator_email": "eval@landing.com",
        "overall_score": 92.0,
        "dialpad_link": "https://dialpad.com/callhistory/callreview/123456",
        "dialpad_entry_point_call_id": "ep-123",
        "dialpad_call_metadata": None,
        "ts": datetime(2026, 4, 1, 9, 0, 0),
        "approved_at": datetime(2026, 4, 1, 10, 0, 0),
        "is_active": True,
        "supervisor": "Sup A",
    }
    row.update(over)
    return row


def _sec_row(eval_id, section_id, *, numeric=None, binary=None) -> dict:
    return {
        "evaluation_id": eval_id,
        "section_id": section_id,
        "numeric_score": numeric,
        "binary_value": binary,
    }


def _identity_alias(config) -> dict[str, str]:
    """Current-config slice of _section_alias_map — every live section_id
    and history_id resolves to its own history_id."""
    alias: dict[str, str] = {}
    for s in config.sections_by_number:
        alias[s.history_id] = s.history_id
        alias[s.id] = s.history_id
    return alias


# ---------------------------------------------------------------------------
# eval_id parsing (parity trap §4.2)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("link,expected", [
    ("https://dialpad.com/callhistory/callreview/123456", "123456"),
    ("https://dialpad.com/callhistory/callreview/123456?foo=bar", "123456"),
    ("https://dialpad.com/callhistory/callreview/123456 [LONG CALL]", "123456"),
    ("https://dialpad.com/callhistory/callreview/123456/", "123456"),
    ("", ""),
])
def test_eval_id_from_link(link, expected):
    assert _eval_id_from_link(link) == expected


def test_eval_id_fallback_chain(sales):
    """link text → superseded metadata link → entry-point call id."""
    alias = _identity_alias(sales)
    a = _eval_row(id=1, dialpad_link="https://x/callhistory/callreview/AAA")
    b = _eval_row(
        id=2, dialpad_link=None,
        dialpad_call_metadata=json.dumps(
            {"backfill": {"superseded_dialpad_link": "https://x/callhistory/callreview/BBB"}}
        ),
    )
    c = _eval_row(id=3, dialpad_link=None, dialpad_call_metadata=None,
                  dialpad_entry_point_call_id="CCC")
    df = frame_from_rows(sales, [a, b, c], [], alias).set_index("eval_id")
    assert set(df.index) == {"AAA", "BBB", "CCC"}


# ---------------------------------------------------------------------------
# Schema parity — the load-bearing invariant for the flip
# ---------------------------------------------------------------------------

def test_frame_schema_matches_load_and_clean(config):
    """The nine compute_* functions run unchanged only if frame_from_rows
    emits the SAME column set load_and_clean derives from the sheet."""
    history = make_history_sheet(config)
    mails = make_mails_sheet(
        ["Star Rep", "Decline Rep", "Improve Rep", "Steady Rep", "Junior Rep"])
    sheet_df = load_and_clean(history, mails, config)

    num = config.numeric_history_ids
    yn = config.yn_history_ids
    secs = []
    if num:
        secs.append(_sec_row(1, num[0], numeric=3.0))
    if yn:
        secs.append(_sec_row(1, yn[0], binary="Y"))
    pg_df = frame_from_rows(config, [_eval_row(id=1)], secs, _identity_alias(config))

    assert not sheet_df.empty and not pg_df.empty
    assert set(pg_df.columns) == set(sheet_df.columns)


# ---------------------------------------------------------------------------
# Departed agents + roster filters (parity traps §4.3 / §4.4)
# ---------------------------------------------------------------------------

def test_departed_agent_degrades(sales):
    """agent_id NULL (LEFT JOIN miss) → inactive, blank supervisor, raw name."""
    ev = _eval_row(agent_display=None, agent_name_raw="Departed Person",
                   is_active=False, supervisor="")
    df = frame_from_rows(sales, [ev], [], _identity_alias(sales))
    assert len(df) == 1
    r = df.iloc[0]
    assert r["agent"] == "Departed Person"
    assert bool(r["is_active"]) is False
    assert r["supervisor"] == ""


def test_excluded_test_agent_dropped(sales):
    """Real Sales config excludes 'Maximiliano Perez'."""
    ev = _eval_row(agent_display="Maximiliano Perez", agent_name_raw="Maximiliano Perez")
    df = frame_from_rows(sales, [ev], [], _identity_alias(sales))
    assert df.empty


# ---------------------------------------------------------------------------
# Row-drop rules (parity trap §4.6)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_ts", [None, datetime(2019, 12, 31, 23, 59)])
def test_bad_timestamp_dropped(sales, bad_ts):
    ev = _eval_row(ts=bad_ts)
    assert frame_from_rows(sales, [ev], [], _identity_alias(sales)).empty


# ---------------------------------------------------------------------------
# Section values: numeric NA → NaN, Y/N vocab, missing-section defaults
# ---------------------------------------------------------------------------

def test_numeric_na_becomes_nan(sales):
    num = sales.numeric_history_ids
    df = frame_from_rows(
        sales, [_eval_row(id=5)], [_sec_row(5, num[0], numeric=None)],
        _identity_alias(sales))
    assert math.isnan(df.iloc[0][num[0]])


def test_missing_numeric_section_is_nan(sales):
    num = sales.numeric_history_ids
    df = frame_from_rows(sales, [_eval_row(id=6)], [], _identity_alias(sales))
    assert math.isnan(df.iloc[0][num[0]])


def test_yn_vocab_and_missing_default(sales):
    yn = sales.yn_history_ids
    if not yn:
        pytest.skip("Sales config has no Y/N sections")
    df = frame_from_rows(
        sales, [_eval_row(id=7)], [_sec_row(7, yn[0], binary="N")],
        _identity_alias(sales))
    assert df.iloc[0][yn[0]] == "N"
    df2 = frame_from_rows(sales, [_eval_row(id=8)], [], _identity_alias(sales))
    assert df2.iloc[0][yn[0]] == ""


# ---------------------------------------------------------------------------
# Timestamps: naive, tz stripped (parity trap §4.1)
# ---------------------------------------------------------------------------

def test_timestamps_are_naive(sales):
    df = frame_from_rows(sales, [_eval_row(id=9)], [], _identity_alias(sales))
    assert df["timestamp"].dt.tz is None
    assert df["eval_approved_at"].dt.tz is None


# ---------------------------------------------------------------------------
# Positional section aliasing across archived rubrics (team_source docstring)
# ---------------------------------------------------------------------------

def test_section_alias_positional(sales):
    """A renamed slot in an archived rubric aliases to the CURRENT section
    occupying the same section_number — historic scores stay in-column."""
    target = sales.sections_by_number[0]
    archived = {"sections": [{
        "id": "legacy_slot_id",
        "section_number": target.section_number,
        "history_id": "legacy_hist",
    }]}

    class _FakeConn:
        async def fetch(self, _query, *_args):
            return [{"rubric_json": json.dumps(archived)}]

    alias = asyncio.run(_section_alias_map(_FakeConn(), sales))
    # archived id + its history_id both resolve to the current slot's history_id
    assert alias["legacy_slot_id"] == target.history_id
    assert alias["legacy_hist"] == target.history_id
    # current ids resolve to themselves
    assert alias[target.history_id] == target.history_id
    assert alias[target.id] == target.history_id

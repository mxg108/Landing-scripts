"""B1 import mapping — staged rows → qa.* insert parameters.

Covers the pure translation layer (score_type matrix, NA shapes per
migration 012, ai_provider-iff-ai CHECK alignment). The asyncpg loop is
deliberately thin; its behavior is exercised by dry-run/smoke runs
against the real staging files.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "backfill_seed_b1.py"
spec = importlib.util.spec_from_file_location("backfill_seed_b1", _SCRIPT)
b1 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b1)


def staged_eval(sections):
    return {"sections": sections}


def sec(kind, era, value, *, score_source="manual", confidence=None, reasoning=None):
    return {"section_id": f"{kind}_{era}", "kind": kind, "era": era,
            "value": value, "score_source": score_source,
            "confidence": confidence, "reasoning": reasoning}


def test_score_type_matrix():
    rows = b1._section_params(1, staged_eval([
        sec("numeric", "ai", {"numeric": 4}),
        sec("numeric", "manual", {"numeric": 3}),
        sec("yn", "ai", {"binary": "Y"}),
        sec("yn", "manual", {"binary": "N"}),
        sec("manual", "ai", {"numeric": 5}),
        sec("manual_yn", "manual", {"binary": "Y"}),
    ]))
    assert [r[3] for r in rows] == [
        "numeric", "manual_numeric", "binary", "manual_binary",
        "manual_numeric", "manual_binary",
    ]
    assert [r[2] for r in rows] == [1, 2, 3, 4, 5, 6]  # section_number order


def test_numeric_na_uses_migration_012_shape():
    [row] = b1._section_params(1, staged_eval([
        sec("numeric", "ai", {"na": True}),
    ]))
    numeric_score, binary_value = row[4], row[5]
    assert numeric_score is None and binary_value == "NA"


def test_binary_values_land_in_binary_column():
    rows = b1._section_params(1, staged_eval([
        sec("yn", "ai", {"binary": "Y"}),
        sec("yn", "ai", {"na": True}),
    ]))
    assert [(r[4], r[5]) for r in rows] == [(None, "Y"), (None, "NA")]


def test_ai_provider_iff_ai_score_source():
    rows = b1._section_params(1, staged_eval([
        sec("numeric", "ai", {"numeric": 4}, score_source="ai",
            confidence="HIGH", reasoning="because"),
        sec("manual", "ai", {"numeric": 2}, score_source="manual"),
    ]))
    ai_row, manual_row = rows
    assert (ai_row[7], ai_row[8]) == ("gemini", "gemini-2.5-flash")
    assert (manual_row[7], manual_row[8]) == (None, None)
    assert ai_row[9] == "HIGH" and ai_row[10] == "because"


def test_ts_roundtrip():
    dt = b1._ts("2026-05-01T10:00:00+00:00")
    assert dt.tzinfo is not None and dt.year == 2026
    assert b1._ts(None) is None


def test_v0_sheet_formula_files_declare_expected_ids():
    for team, version in b1.FORMULA_VERSION.items():
        import json
        payload = json.loads(b1.V0_SHEET_JSON[team].read_text(encoding="utf-8"))
        assert payload.get("formula_id") == version, (
            f"{team}: v0_sheet.json declares {payload.get('formula_id')!r}; "
            f"B1's FK ship step expects {version!r}"
        )

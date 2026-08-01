"""HR bonus sheet backend — config block, month payload, endpoint.

Spec: references/HRBonusSheet.md. P1 checkpoint tests cover the
`hr_export` config block and its internal-only invariant; P2 covers the
aggregation math, month bucketing (incl. the DST boundary), the
no-prior-month-leakage rule, and — load-bearing — golden parity between
the service and the HR-approved mockup exporter on identical data.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.config.team_config import TeamConfig, _assemble_rubric_block
from backend.middleware import auth
from backend.middleware.auth import KeyIdentity, TEAM_AUTH_DEPENDENCY
from backend.routes import hr_bonus as hr_bonus_route
from backend.services.hr_bonus_service import (
    build_payload_from_rows,
    month_window_utc,
)
from tests.conftest import load_test_config

_LA = ZoneInfo("America/Los_Angeles")
_AI_SCORING = Path(__file__).resolve().parent.parent

MS_HR_SECTION_IDS = [
    "greeting", "caller_id", "purpose", "matching", "process_adherence",
    "call_resolution", "comms", "efficiency", "cri",
]


def _ms_raw() -> dict:
    """Prod MS JSON, loader-normalized, ready for mutation + TeamConfig()."""
    path = _AI_SCORING / "backend" / "config" / "teams" / "member_support.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["rubric"] = _assemble_rubric_block(raw)
    for legacy_key in ("rubric_version", "sections", "scoring_prompt"):
        raw.pop(legacy_key, None)
    return raw


# ---------------------------------------------------------------------------
# P1 — hr_export config block
# ---------------------------------------------------------------------------

def test_shipped_ms_config_exports_exactly_the_nine_spec_sections():
    config = load_test_config("member_support")
    assert config.hr_export is not None
    assert [s.id for s in config.hr_export.sections] == MS_HR_SECTION_IDS
    assert config.hr_export.sections[0].hr_label == "Greeting"
    assert config.hr_export.sections[-1].hr_label == "CRI"


def test_shipped_ms_config_never_exports_human_review_required():
    config = load_test_config("member_support")
    assert "human_review_required" in config.hr_internal_section_ids
    assert "human_review_required" not in {s.id for s in config.hr_export.sections}


def test_teams_without_hr_export_load_fine():
    config = load_test_config("sales")
    assert config.hr_export is None


def test_internal_only_section_in_hr_export_is_rejected():
    raw = _ms_raw()
    raw["hr_export"]["sections"].append(
        {"id": "human_review_required", "hr_label": "HRR"}
    )
    with pytest.raises(ValidationError, match="internal-only"):
        TeamConfig(**raw)


def test_unknown_section_id_in_hr_export_is_rejected():
    raw = _ms_raw()
    raw["hr_export"]["sections"].append({"id": "nonexistent", "hr_label": "X"})
    with pytest.raises(ValidationError, match="not a rubric section"):
        TeamConfig(**raw)


def test_duplicate_section_id_in_hr_export_is_rejected():
    raw = _ms_raw()
    raw["hr_export"]["sections"].append({"id": "greeting", "hr_label": "Greeting 2"})
    with pytest.raises(ValidationError, match="listed twice"):
        TeamConfig(**raw)


def test_empty_hr_export_sections_is_rejected():
    raw = _ms_raw()
    raw["hr_export"]["sections"] = []
    with pytest.raises(ValidationError, match="must not be empty"):
        TeamConfig(**raw)


# ---------------------------------------------------------------------------
# P2 — month window (project bucket TZ)
# ---------------------------------------------------------------------------

def test_month_window_is_bucket_tz_not_utc():
    start, end = month_window_utc("2026-06")
    # June 1 midnight in LA is 07:00 UTC during DST.
    assert start == datetime(2026, 6, 1, 7, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 7, 1, 7, 0, tzinfo=timezone.utc)


def test_dst_boundary_call_buckets_into_prior_month():
    # 2026-06-01 03:33 UTC is May 31 20:33 in LA — a May call.
    ts = datetime(2026, 6, 1, 3, 33, tzinfo=timezone.utc)
    may_start, may_end = month_window_utc("2026-05")
    june_start, _ = month_window_utc("2026-06")
    assert may_start <= ts < may_end
    assert ts < june_start


def test_winter_month_window_uses_standard_offset():
    start, _ = month_window_utc("2026-01")
    assert start == datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc)  # PST = UTC-8


def test_december_window_rolls_the_year():
    _, end = month_window_utc("2026-12")
    assert end == datetime(2027, 1, 1, 8, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# P2 — payload assembly
# ---------------------------------------------------------------------------

def _eval_row(eval_id, agent, ts, overall=90.0, email="a@landing.com",
              evaluator="boss@landing.com", link="https://dialpad.com/call/1"):
    return {
        "id": eval_id, "agent_name_raw": agent, "agent_email": email,
        "evaluator_email": evaluator, "ts": ts, "overall_score": overall,
        "dialpad_link": link,
    }


def _num(section_id, value):
    return {"section_id": section_id, "numeric_score": value, "binary_value": None}


def _bin(section_id, value):
    return {"section_id": section_id, "numeric_score": None, "binary_value": value}


def _june(day, hour=12):
    """LA wall-clock June 2026 instant, as UTC."""
    return datetime(2026, 6, day, hour, tzinfo=_LA).astimezone(timezone.utc)


@pytest.fixture
def ms_config():
    return load_test_config("member_support")


def test_summary_aggregation_math(ms_config):
    evals = [_eval_row(1, "Ana", _june(10), overall=88.0),
             _eval_row(2, "Ana", _june(5), overall=91.0)]
    sections = {
        1: {"greeting": _num("greeting", 4), "caller_id": _bin("caller_id", "Y"),
            "cri": _bin("cri", "NA")},
        2: {"greeting": _num("greeting", 5), "caller_id": _bin("caller_id", "N"),
            "cri": _bin("cri", "Y")},
    }
    payload = build_payload_from_rows(ms_config, "2026-06", evals, sections)
    [agent] = payload["agents"]
    assert agent["monthly_avg"] == 89.5
    summaries = dict(zip([s.id for s in ms_config.hr_export.sections],
                         agent["section_summaries"]))
    assert summaries["greeting"] == "4.50"
    assert summaries["caller_id"] == "50%"
    assert summaries["cri"] == "100%"      # NA excluded from the denominator
    assert summaries["purpose"] == ""      # no data → blank, never 0


def test_detail_rows_render_and_order(ms_config):
    evals = [_eval_row(1, "Ana", _june(5, hour=9), overall=88.0),
             _eval_row(2, "Ana", _june(20, hour=15), overall=91.0)]
    sections = {
        1: {"greeting": _num("greeting", 3), "caller_id": _bin("caller_id", "NA")},
        2: {"greeting": _num("greeting", 5), "caller_id": _bin("caller_id", "Y")},
    }
    payload = build_payload_from_rows(ms_config, "2026-06", evals, sections)
    rows = payload["agents"][0]["evaluations"]
    assert [r["date"] for r in rows] == ["06/20/2026 15:00", "06/05/2026 09:00"]  # newest first
    assert rows[0]["period"] == "2026-06"
    cells = dict(zip([s.id for s in ms_config.hr_export.sections], rows[0]["sections"]))
    assert cells["greeting"] == "5"
    assert cells["caller_id"] == "Yes"
    assert cells["purpose"] == ""
    older = dict(zip([s.id for s in ms_config.hr_export.sections], rows[1]["sections"]))
    assert older["caller_id"] == "N/A"


def test_prior_month_rows_never_leak(ms_config):
    evals = [_eval_row(1, "Ana", _june(10)),
             _eval_row(2, "Ana", datetime(2026, 5, 20, 19, 0, tzinfo=timezone.utc))]
    payload = build_payload_from_rows(ms_config, "2026-06", evals, {})
    [agent] = payload["agents"]
    assert len(agent["evaluations"]) == 1
    assert agent["evaluations"][0]["period"] == "2026-06"


def test_excluded_agents_filtered_case_insensitively(ms_config):
    evals = [_eval_row(1, "Ana", _june(10)),
             _eval_row(2, "Maximiliano Perez", _june(11))]
    payload = build_payload_from_rows(ms_config, "2026-06", evals, {})
    assert [a["name"] for a in payload["agents"]] == ["Ana"]


def test_qa_roster_spelling_of_operator_is_excluded(ms_config):
    # Matching is exact-after-lowercase (no accent stripping): the qa.*
    # roster spells the operator "Max Pérez", which the mockup-era entries
    # missed — caught live in the July 2026 export. The shipped config must
    # carry the roster spelling explicitly.
    evals = [_eval_row(1, "Ana", _june(10)),
             _eval_row(2, "Max Pérez", _june(11))]
    payload = build_payload_from_rows(ms_config, "2026-06", evals, {})
    assert [a["name"] for a in payload["agents"]] == ["Ana"]


def test_agents_sorted_case_insensitively(ms_config):
    evals = [_eval_row(1, "zoe", _june(10)),
             _eval_row(2, "Ana", _june(11)),
             _eval_row(3, "Bruno", _june(12))]
    payload = build_payload_from_rows(ms_config, "2026-06", evals, {})
    assert [a["name"] for a in payload["agents"]] == ["Ana", "Bruno", "zoe"]


def test_payload_header_fields(ms_config):
    payload = build_payload_from_rows(ms_config, "2026-06", [], {})
    assert payload["month"] == "2026-06"
    assert payload["month_label"] == "June 2026"
    assert payload["team_id"] == "member_support"
    assert payload["section_labels"] == [
        "Greeting", "Caller ID", "Purpose of the call", "Matching the moment",
        "Process Adherence", "Call Resolution", "Communication",
        "Efficiency & Call Handling", "CRI",
    ]
    assert payload["agents"] == []


# ---------------------------------------------------------------------------
# P2 — golden parity with the HR-approved mockup exporter
# ---------------------------------------------------------------------------

def _load_mockup_module():
    path = _AI_SCORING / "scripts" / "export_hr_bonus_sheet.py"
    spec = importlib.util.spec_from_file_location("export_hr_bonus_sheet", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# mockup CSV column name -> (section id, DB shape) per HRBonusSheet.md §3
_CSV_TO_DB = [
    ("Greeting", "greeting", "num"),
    ("Caller Identity Validation", "caller_id", "bin"),
    ("Purpose of the Call", "purpose", "num"),
    ("Matching the Moment", "matching", "num"),
    ("Process Adherence", "process_adherence", "num"),
    ("Call Resolution", "call_resolution", "num"),
    ("Communication", "comms", "num"),
    ("Efficiency & Call Handling", "efficiency", "num"),
    ("Customer Resolution Indicator", "cri", "bin"),
]
_CSV_BINARY = {"Y": "Yes", "N": "No", "NA": "Not Applicable"}


def test_golden_parity_with_mockup_exporter(ms_config):
    """The DB-sourced service must reproduce the numbers HR approved.

    Two agents, five evaluations, mixed numeric/binary/NA/missing values.
    The same dataset is fed to the mockup's _summary_row/_detail_rows
    (CSV shape) and to build_payload_from_rows (DB shape); every summary
    value and detail cell must agree.
    """
    import pandas as pd

    mockup = _load_mockup_module()

    dataset = [
        # (agent, email, LA day, overall, {section_id: value})
        ("Ana López", "ana@landing.com", 22, 93.0,
         {"greeting": 5, "caller_id": "Y", "purpose": 4, "matching": 5,
          "process_adherence": 4, "call_resolution": 5, "comms": 4,
          "efficiency": 3, "cri": "Y"}),
        ("Ana López", "ana@landing.com", 15, 88.0,
         {"greeting": 4, "caller_id": "N", "purpose": 3, "matching": 4,
          "process_adherence": 5, "call_resolution": 4, "comms": 5,
          "efficiency": 4, "cri": "NA"}),
        ("Ana López", "ana@landing.com", 3, 76.5,
         {"greeting": 3, "caller_id": "Y", "purpose": 5, "matching": 3,
          "process_adherence": 3, "call_resolution": 3, "comms": 3,
          "efficiency": 5, "cri": "N"}),
        ("bruno díaz", "bruno@landing.com", 18, 81.0,
         {"greeting": 4, "caller_id": "NA", "purpose": 4, "matching": 2,
          "call_resolution": 5, "comms": 4, "efficiency": 4, "cri": "Y"}),
        ("bruno díaz", "bruno@landing.com", 9, 84.5,
         {"greeting": 2, "caller_id": "Y", "purpose": 5, "matching": 4,
          "call_resolution": 4, "comms": 5, "efficiency": 3, "cri": "Y"}),
    ]

    # --- CSV/DataFrame shape for the mockup ------------------------------
    csv_rows = []
    for agent, email, day, overall, values in dataset:
        wall = datetime(2026, 6, day, 12, 0)
        row = {
            "Agent Name": agent, "Agent Email": email,
            "Overall Score": overall,
            "Evaluator Email": "boss@landing.com",
            "Dialpad Link": f"https://dialpad.com/call/{day}",
            "_ts": pd.Timestamp(wall), "_period": "2026-06", "_agent": agent,
        }
        for csv_col, sec_id, shape in _CSV_TO_DB:
            v = values.get(sec_id)
            if v is None:
                row[csv_col] = ""
            elif shape == "bin":
                row[csv_col] = _CSV_BINARY[v]
            else:
                row[csv_col] = v
        csv_rows.append(row)
    df = pd.DataFrame(csv_rows).sort_values("_ts", ascending=False)

    # --- DB shape for the service ----------------------------------------
    eval_rows, sections_by_eval = [], {}
    for i, (agent, email, day, overall, values) in enumerate(dataset, start=1):
        eval_rows.append(_eval_row(
            i, agent, _june(day), overall=overall, email=email,
            link=f"https://dialpad.com/call/{day}",
        ))
        sections_by_eval[i] = {}
        for _, sec_id, shape in _CSV_TO_DB:
            v = values.get(sec_id)
            if v is None:
                continue
            sections_by_eval[i][sec_id] = (
                _bin(sec_id, v) if shape == "bin" else _num(sec_id, v)
            )

    payload = build_payload_from_rows(ms_config, "2026-06", eval_rows, sections_by_eval)

    # --- Compare, agent by agent ------------------------------------------
    assert [a["name"] for a in payload["agents"]] == ["Ana López", "bruno díaz"]
    for agent_payload in payload["agents"]:
        rows = df[df["_agent"] == agent_payload["name"]]
        expected = mockup._summary_row(agent_payload["name"], rows)

        assert agent_payload["email"] == expected[1]
        assert agent_payload["monthly_avg"] == expected[2]
        for got, want in zip(agent_payload["section_summaries"], expected[3:]):
            if isinstance(want, float):
                assert float(got) == pytest.approx(want)
            else:
                assert got == want  # "%Yes" strings and blanks

        detail = mockup._detail_rows(rows)
        header, mock_rows = detail[0], detail[1:]
        assert len(mock_rows) == len(agent_payload["evaluations"])
        for mock_row, got in zip(mock_rows, agent_payload["evaluations"]):
            assert got["period"] == mock_row[0]
            assert got["date"] == mock_row[1]
            assert float(got["overall_score"]) == float(mock_row[2])
            assert got["sections"] == [str(c) for c in mock_row[3:12]]
            assert got["evaluator"] == mock_row[12]
            assert got["dialpad_link"] == mock_row[13]


# ---------------------------------------------------------------------------
# P2 — endpoint
# ---------------------------------------------------------------------------

MS_TOKEN = "team-ms-tok"
SALES_TOKEN = "team-sales-tok"


@pytest.fixture
def hr_client(monkeypatch):
    monkeypatch.setattr(auth, "_KEY_MAP", {
        MS_TOKEN: KeyIdentity(role="team", team_id="member_support"),
        SALES_TOKEN: KeyIdentity(role="team", team_id="sales"),
    })
    app = FastAPI()
    app.include_router(
        hr_bonus_route.router,
        prefix="/api/{team_id}",
        dependencies=TEAM_AUTH_DEPENDENCY,
    )
    return TestClient(app)


def _get(client, team, month, token):
    return client.get(
        f"/api/{team}/hr-bonus/{month}",
        headers={"Authorization": f"Bearer {token}"},
    )


def test_endpoint_returns_service_payload(hr_client, monkeypatch):
    async def fake_fetch(config, month):
        return {"team_id": config.team_id, "month": month, "agents": []}
    monkeypatch.setattr(hr_bonus_route, "fetch_month_payload", fake_fetch)
    resp = _get(hr_client, "member_support", "2026-06", MS_TOKEN)
    assert resp.status_code == 200
    assert resp.json() == {"team_id": "member_support", "month": "2026-06", "agents": []}


def test_endpoint_rejects_malformed_month(hr_client):
    for bad in ("2026-13", "junk", "2026-6", "2026-06-01"):
        resp = _get(hr_client, "member_support", bad, MS_TOKEN)
        assert resp.status_code == 422, bad


def test_endpoint_404s_for_team_without_hr_export(hr_client):
    resp = _get(hr_client, "sales", "2026-06", SALES_TOKEN)
    assert resp.status_code == 404
    assert "no HR export" in resp.json()["detail"]


def test_endpoint_503s_without_database(hr_client, monkeypatch):
    async def fake_fetch(config, month):
        return None
    monkeypatch.setattr(hr_bonus_route, "fetch_month_payload", fake_fetch)
    resp = _get(hr_client, "member_support", "2026-06", MS_TOKEN)
    assert resp.status_code == 503


def test_endpoint_requires_matching_team_key(hr_client):
    resp = _get(hr_client, "member_support", "2026-06", SALES_TOKEN)
    assert resp.status_code in (401, 403)

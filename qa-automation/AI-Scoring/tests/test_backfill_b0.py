"""B0 sanitize+stage — BackfillPlan.md §4 rules on synthetic seed rows.

Drives the pure core (`stage_rows`) with in-memory CSV rows; the real
seed CSVs are gitignored PII and never touched by the suite. The two
real-seed invariants (1,671 / 333 staged, 3 / 13 anomalies) live in the
B0 run report, not here.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "backfill_seed_b0.py"
spec = importlib.util.spec_from_file_location("backfill_seed_b0", _SCRIPT)
b0 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b0)

MS_SECTIONS = b0.TEAM_SECTIONS["member_support"]
N = len(MS_SECTIONS)


def ms_columns() -> list[str]:
    names = ["Greeting", "Caller Identity Validation", "Purpose of the Call",
             "Matching the Moment", "Process Adherence", "Call Resolution",
             "Communication", "Efficiency & Call Handling", "Documentation",
             "Customer Resolution Indicator"]
    return (["Agent Name", "Agent Email", "Timestamp", "Evaluator Email",
             "Dialpad Link", "Overall Score"]
            + names + [f"{n} reasoning" for n in names]
            + [f"{n} confidence" for n in names]
            + ["Key Strengths", "Opportunities", "Call Summary",
               "Caller Name", "Caller Phone", "Source", "Unnamed: 42"])


def ms_row(*, agent="Jane Doe", ts="2026-05-01 10:00:00", link="https://d/c/1",
           overall=None, scores=None, source="ai", approval="2026-05-02 09:00:00",
           reasoning="because", confidence="high") -> dict:
    """A consistent AI-era row. scores: dict section_name->cell; defaults
    produce a row that matches the v0_sheet formula exactly."""
    cols = ms_columns()
    row = {c: "" for c in cols}
    defaults = {"Greeting": "5", "Caller Identity Validation": "Yes",
                "Purpose of the Call": "5", "Matching the Moment": "5",
                "Process Adherence": "5", "Call Resolution": "5",
                "Communication": "5", "Efficiency & Call Handling": "5",
                "Documentation": "5", "Customer Resolution Indicator": "Yes"}
    defaults.update(scores or {})
    row.update({"Agent Name": agent, "Timestamp": ts, "Dialpad Link": link,
                "Evaluator Email": "boss@landing.com", "Source": source,
                "Unnamed: 42": approval})
    for name, val in defaults.items():
        row[name] = val
        row[f"{name} reasoning"] = reasoning
        row[f"{name} confidence"] = confidence
    exact = b0.v0_sheet_score("member_support", {
        sec_id: b0.normalize_cell(defaults[name], kind)
        for (sec_id, kind), name in zip(MS_SECTIONS, defaults)
    })
    row["Overall Score"] = str(round(exact)) if overall is None else str(overall)
    return row


def run(rows):
    return b0.stage_rows("member_support", rows, ms_columns())


# ---------------------------------------------------------------------------

def test_clean_row_stages_with_expected_shape():
    out = run([ms_row()])
    assert out["counts"]["rows_staged"] == 1 and not out["excluded"]
    s = out["staged"][0]
    assert s["state"] == "finalized"
    assert s["source"] == "ai_reviewed"           # Source='ai' → ai_reviewed
    assert s["formula_version"] == "member_support_v0_sheet"
    assert s["rubric_version"] == "member_support_v1"
    assert s["models_used"]["text"]["provider"] == "gemini"
    assert len(s["sections"]) == N
    assert not s["annotations"] and not s["import_blocked"]


def test_binary_and_confidence_normalization():
    out = run([ms_row(scores={"Caller Identity Validation": "Not Applicable",
                              "Customer Resolution Indicator": "No"})])
    by_id = {sec["section_id"]: sec for sec in out["staged"][0]["sections"]}
    assert by_id["caller_identity_validation"]["value"] == {"na": True}
    assert by_id["customer_resolution_indicator"]["value"] == {"binary": "N"}
    assert by_id["greeting"]["confidence"] == "HIGH"   # 'high' → 'HIGH'


def test_manual_era_never_fabricates_reasoning():
    out = run([ms_row(source="", reasoning="stray text", confidence="high")])
    s = out["staged"][0]
    assert s["source"] == "manual"
    assert s["models_used"]["text"] == {"provider": "human", "model": "human_brain"}
    for sec in s["sections"]:
        assert sec["reasoning"] is None and sec["confidence"] is None  # §7
        assert sec["score_source"] == "manual"


def test_hard_zero_override_excluded():
    out = run([ms_row(overall=0)])
    assert out["counts"]["rows_staged"] == 0
    assert out["excluded"][0]["reason"] == "manual_hard_zero_override"


def test_all_zero_no_agent_test_artifact_excluded():
    zeros = {name: ("1" if kind in ("numeric", "manual") else "No")
             for (sec_id, kind), name in zip(MS_SECTIONS, [
                 "Greeting", "Caller Identity Validation", "Purpose of the Call",
                 "Matching the Moment", "Process Adherence", "Call Resolution",
                 "Communication", "Efficiency & Call Handling", "Documentation",
                 "Customer Resolution Indicator"])}
    # all-zero frac: ratings of 0 and binary No
    zeros = {k: ("0" if v == "1" else "No") for k, v in zeros.items()}
    out = run([ms_row(agent="", overall=0, scores=zeros)])
    assert out["excluded"][0]["reason"] == "all_zero_test_artifact"


def test_corrupt_section_value_excluded():
    out = run([ms_row(scores={"Greeting": "banana"})])
    assert out["counts"]["rows_staged"] == 0
    assert out["excluded"][0]["reason"].startswith("corrupt_section_value")


def test_hand_edit_gets_anomaly_annotation():
    out = run([ms_row(overall=42)])  # formula-consistent default is 100
    s = out["staged"][0]
    assert "backfill_anomaly" in s["annotations"]
    assert out["counts"]["anomalies"] == 1


def test_na_redistribution_matches_formula():
    # CRI (weight 1) goes NA — remaining weights rescale; all-5s still = 100.
    out = run([ms_row(scores={"Customer Resolution Indicator": "Not Applicable"})])
    assert out["counts"]["anomalies"] == 0


def test_broken_timestamp_goes_to_repair_queue_not_exclusion():
    out = run([ms_row(ts="12/31/1969 18:00:00")])
    s = out["staged"][0]
    assert s["call_connected_at"] is None
    assert "backfill_broken_timestamp" in s["annotations"]
    assert not s["import_blocked"]                 # approval clock still usable


def test_no_usable_clock_blocks_import():
    out = run([ms_row(ts="garbage", approval="")])
    s = out["staged"][0]
    assert s["import_blocked"] and s["approved_at"] is None
    assert out["counts"]["import_blocked_no_clock"] == 1


def test_missing_approval_clock_falls_back_to_timestamp():
    out = run([ms_row(approval="")])
    s = out["staged"][0]
    assert s["approved_at"] == s["call_connected_at"]
    assert s["annotations"]["backfill_approved_at_fallback"] == "timestamp_col"


def test_duplicate_link_latest_keeps_it():
    older = ms_row(approval="2026-05-02 09:00:00")
    newer = ms_row(approval="2026-06-01 09:00:00")
    out = run([older, newer])
    assert out["counts"]["rows_staged"] == 2       # D2: import all
    with_link = [s for s in out["staged"] if s["dialpad_link"]]
    superseded = [s for s in out["staged"] if not s["dialpad_link"]]
    assert len(with_link) == 1 and with_link[0]["approved_at"].startswith("2026-06-01")
    assert superseded[0]["annotations"]["superseded_dialpad_link"] == "https://d/c/1"


def test_reeval_rows_get_distinct_natural_keys():
    # Same call clock + link + agent (post-PR-1 re-eval shape) but
    # different approval clocks — keys must not collide.
    rows = [ms_row(approval="2026-05-02 09:00:00"),
            ms_row(approval="2026-06-01 09:00:00")]
    out = run(rows)
    keys = {s["natural_key"] for s in out["staged"]}
    assert len(keys) == 2


def test_identical_rows_get_ordinal_keys():
    out = run([ms_row(), ms_row()])
    keys = {s["natural_key"] for s in out["staged"]}
    assert len(keys) == 2


def test_migrated_source_maps_to_manual_with_provenance():
    out = run([ms_row(source="migrated")])
    s = out["staged"][0]
    assert s["source"] == "manual"
    assert s["annotations"]["backfill_source_raw"] == "migrated"


def test_width_mismatch_is_structural_failure():
    with pytest.raises(SystemExit, match="structural"):
        b0.stage_rows("member_support", [], ms_columns()[:-1])

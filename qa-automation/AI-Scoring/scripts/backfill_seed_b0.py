#!/usr/bin/env python3
"""B0 — sanitize + stage the Analyst_History seed (BackfillPlan.md §4/§5).

Offline, no DB, pure and re-runnable: reads the team's seed CSV, applies
the §4 normalization rules and §8 decisions, and writes a reviewable
staging file that B1 imports verbatim. Every transform is counted in the
run report; nothing touches Railway.

Outputs (under --out-dir, default database/backfill_staging/):
    staging_<team>.jsonl            one object per importable evaluation
    backfill_exclusions_<team>.csv  excluded rows + reason (§8 D5 etc.)
    report_<team>_b0.json           machine-readable run report
    (+ a printed human summary)

Deterministic: same CSV in, byte-identical staging out — safe to re-run
any time and diff. The natural key (team | dialpad_link | raw timestamp
| agent) rides along so B1 upserts instead of duplicating.

Usage:
    cd qa-automation/AI-Scoring
    python3 scripts/backfill_seed_b0.py --team-id member_support
    python3 scripts/backfill_seed_b0.py --team-id sales --csv path.csv

Exit codes: 0 = staged, gates green; 1 = structural failure (nothing
written); 2 = staged but expectation gates drifted (review report).
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

_AI_SCORING = Path(__file__).resolve().parent.parent
_REPO_ROOT = _AI_SCORING.parent.parent
_DEFAULT_OUT = _REPO_ROOT / "database" / "backfill_staging"

# ---------------------------------------------------------------------------
# Frozen per-team facts (archived v1 rubrics + reverse-engineered v0_sheet
# formulas — BackfillPlan §2/§2a/§3). Deliberately hardcoded: this is
# settled history keyed to the migration-010 archive, not live config.
# ---------------------------------------------------------------------------

# (archived section_id, kind) in CSV column order; kind: "numeric" | "yn"
# | "manual" (analyst-filled 1-5) | "manual_yn" (analyst-filled Y/N/NA).
TEAM_SECTIONS: dict[str, list[tuple[str, str]]] = {
    "member_support": [
        ("greeting", "numeric"),
        ("caller_identity_validation", "yn"),
        ("purpose_of_call", "numeric"),
        ("matching_the_moment", "numeric"),
        ("process_adherence", "numeric"),
        ("call_resolution", "numeric"),
        ("communication", "numeric"),
        ("efficiency_call_handling", "numeric"),
        ("documentation", "manual"),
        ("customer_resolution_indicator", "yn"),
    ],
    "sales": [
        ("greeting", "yn"),
        ("pb_creation", "manual_yn"),
        ("mc_call_notes", "manual_yn"),
        ("situation_match", "numeric"),
        ("reason_for_move_pitch", "yn"),
        ("value_uplift", "numeric"),
        ("membership_explanation", "yn"),
        ("flex_long_stay_pitch", "yn"),
        ("landing_guarantee", "numeric"),
        ("pricing_explanation", "yn"),
        ("book_attempt", "yn"),
        ("objection_handling", "numeric"),
        ("urgency_disclosure", "yn"),
        ("followup_setup", "yn"),
        ("tonality_pace", "yn"),
        ("hold_usage", "yn"),
        ("audio_quality", "yn"),
        ("screen_recording", "yn"),
        ("pre_send_intro", "manual_yn"),
    ],
}

# v0_sheet weights (§2/§2a). overall = 100 × Σ w·frac / Σ w(scored);
# NA/blank sections drop out of both sums (proportional redistribute).
TEAM_WEIGHTS: dict[str, dict[str, float]] = {
    "member_support": {
        "call_resolution": 4, "communication": 2, "efficiency_call_handling": 2,
        "documentation": 2, "purpose_of_call": 1, "customer_resolution_indicator": 1,
        # greeting / caller_identity_validation / matching_the_moment /
        # process_adherence carry weight 0 in the legacy sheet formula.
        "greeting": 0, "caller_identity_validation": 0,
        "matching_the_moment": 0, "process_adherence": 0,
    },
    "sales": {sec_id: (10 if sec_id == "situation_match" else 5)
              for sec_id, _ in TEAM_SECTIONS["sales"]},
}

TEAM_FORMULA_VERSION = {
    "member_support": "member_support_v0_sheet",
    "sales": "sales_v0_sheet",
}
TEAM_RUBRIC_VERSION = {
    "member_support": "member_support_v1",
    "sales": "sales_v1",
}

# Expectation gates from the plan's inventory — drift means the CSV
# changed since the analysis and a human should look before B1.
TEAM_EXPECTATIONS = {
    "member_support": {"rows": 1678, "excluded": 7, "anomalies": 3},
    # Plan §2a expected 2 exclusions, but the CSV on disk is a newer
    # re-export: the corrupted stray-value row was cleaned at the source
    # and an approval-clock column appeared (partially populated). Only
    # the blank-Overall row remains excludable.
    "sales": {"rows": 334, "excluded": 1, "anomalies": 13},
}

BINARY_NORMALIZE = {"Y": "Y", "YES": "Y", "N": "N", "NO": "N",
                    "NOT APPLICABLE": "NA", "NA": "NA", "N/A": "NA"}
CONFIDENCE_NORMALIZE = {"high": "HIGH", "medium": "MED", "med": "MED", "low": "LOW"}

MODELS_USED = {
    "ai": {"text": {"provider": "gemini", "model": "gemini-2.5-flash"}},
    "manual": {"text": {"provider": "human", "model": "human_brain"}},  # §8 D4
}

TS_FORMATS = ["%Y-%m-%d %H:%M:%S", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M",
              "%Y-%m-%dT%H:%M:%S"]
# Sheet clocks are UTC wall time without a marker (house convention);
# anything before this is the epoch-zero corruption class (§1).
MIN_PLAUSIBLE_YEAR = 2020


def parse_ts(raw: str) -> str | None:
    """Sheet timestamp → ISO-8601 UTC string, or None when broken."""
    raw = (raw or "").strip()
    if not raw:
        return None
    for fmt in TS_FORMATS:
        try:
            dt = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        if dt.year < MIN_PLAUSIBLE_YEAR:
            return None  # epoch-zero corruption
        return dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    return None


def normalize_cell(raw: str, kind: str) -> dict:
    """One section cell → staged value dict.

    Shapes: {"numeric": int} | {"binary": "Y"/"N"} | {"na": True} |
    {"missing": True} (blank numeric on a non-NA-vocabulary cell) |
    {"corrupt": raw} (unparseable — triggers row exclusion, §2a).
    Blank cells are NA by sheet semantics (§2a: the sheet treated blank
    exactly like Not Applicable).
    """
    val = (raw or "").strip()
    if not val:
        return {"na": True}
    upper = val.upper()
    if upper in BINARY_NORMALIZE:
        norm = BINARY_NORMALIZE[upper]
        if norm == "NA":
            return {"na": True}
        if kind in ("numeric", "manual"):
            return {"corrupt": raw}  # Y/N in a rating column
        return {"binary": norm}
    try:
        num = float(val)
    except ValueError:
        return {"corrupt": raw}
    if kind in ("yn", "manual_yn"):
        return {"corrupt": raw}  # rating in a binary column
    if not num.is_integer() or not 0 <= num <= 5:
        return {"corrupt": raw}
    return {"numeric": int(num)}


def cell_frac(value: dict) -> float | None:
    """v0_sheet normalization: rating r/5, binary Y=1/N=0, NA/missing None."""
    if "numeric" in value:
        return value["numeric"] / 5.0
    if "binary" in value:
        return 1.0 if value["binary"] == "Y" else 0.0
    return None


def v0_sheet_score(team_id: str, values: dict[str, dict]) -> float | None:
    """Recompute the reverse-engineered legacy sheet score (§2/§2a)."""
    weights = TEAM_WEIGHTS[team_id]
    num = den = 0.0
    for sec_id, value in values.items():
        frac = cell_frac(value)
        if frac is None:
            continue
        w = weights[sec_id]
        num += w * frac
        den += w
    if den == 0:
        return None
    return 100.0 * num / den


def natural_key_basis(team_id: str, link: str, ts_raw: str, agent: str,
                      approval_raw: str, overall_raw: str) -> str:
    """Identity basis for a seed row. Link+timestamp alone is NOT unique:
    col C is the *call* clock (PR-1), so re-evaluations of the same call
    collide, and blank-link manual rows share date-only timestamps. The
    approval clock + overall disambiguate; a per-basis ordinal (appended
    by the caller) covers byte-identical repeats. Stable for a fixed CSV —
    the seed is frozen history."""
    return "|".join([team_id, link.strip(), ts_raw.strip(), agent.strip().lower(),
                     approval_raw.strip(), overall_raw.strip()])


def finalize_natural_keys(staged: list[dict]) -> None:
    """Assign sha1 keys, adding an occurrence ordinal to repeated bases."""
    seen: dict[str, int] = {}
    for s in staged:
        basis = s.pop("_key_basis")
        ordinal = seen.get(basis, 0)
        seen[basis] = ordinal + 1
        s["natural_key"] = hashlib.sha1(
            f"{basis}|{ordinal}".encode("utf-8")).hexdigest()


def stage_rows(team_id: str, rows: list[dict], columns: list[str]) -> dict:
    """Pure core: CSV rows → {staged, excluded, report_counts}."""
    sections = TEAM_SECTIONS[team_id]
    n = len(sections)
    expected_width = 6 + 3 * n + 5 + 1 + 1
    if len(columns) != expected_width:
        raise SystemExit(
            f"structural: {team_id} CSV has {len(columns)} columns, expected {expected_width}"
        )
    col = {name: i for i, name in enumerate(columns)}
    approval_col = columns[-1]  # header-less trailing column (§8 D1)

    staged: list[dict] = []
    excluded: list[dict] = []
    counts = {
        "rows_in": len(rows), "binary_normalized_cells": 0,
        "confidence_normalized_cells": 0, "broken_timestamps": 0,
        "approval_clock_fallbacks": 0, "import_blocked_no_clock": 0,
        "anomalies": 0, "source_migrated_mapped_manual": 0,
        "duplicate_link_groups": 0, "superseded_links": 0,
    }

    def cell(row, name):
        return (row[name] or "").strip()

    for line_no, row in enumerate(rows, start=2):  # 1-based + header
        agent = cell(row, "Agent Name")
        raw_ts = cell(row, "Timestamp")
        link = cell(row, "Dialpad Link")
        overall_raw = cell(row, "Overall Score")

        # ---- section cells -------------------------------------------------
        values: dict[str, dict] = {}
        corrupt = []
        raw_binaries = 0
        for i, (sec_id, kind) in enumerate(sections):
            raw = cell(row, columns[6 + i])
            v = normalize_cell(raw, kind)
            if "corrupt" in v:
                corrupt.append((sec_id, raw))
            if raw.upper() in ("YES", "NO", "NOT APPLICABLE"):
                raw_binaries += 1
            values[sec_id] = v
        counts["binary_normalized_cells"] += raw_binaries

        # ---- exclusions (§1 inventory / §8 D5 / §2a) -----------------------
        def exclude(reason):
            excluded.append({"line": line_no, "agent": agent,
                             "dialpad_link": link, "reason": reason})

        try:
            overall = float(overall_raw)
        except ValueError:
            exclude("missing_or_non_numeric_overall")
            continue
        if corrupt:
            exclude("corrupt_section_value: " +
                    "; ".join(f"{s}={r!r}" for s, r in corrupt))
            continue
        numeric_fracs = [cell_frac(v) for v in values.values()]
        scored = [f for f in numeric_fracs if f is not None]
        if not agent and scored and all(f == 0 for f in scored):
            exclude("all_zero_test_artifact")
            continue
        if overall == 0 and any(f > 0 for f in scored):
            exclude("manual_hard_zero_override")  # §8 D5
            continue

        # ---- clocks (§8 D1) -------------------------------------------------
        annotations: dict = {}
        call_connected_at = parse_ts(raw_ts)
        if call_connected_at is None:
            counts["broken_timestamps"] += 1
            annotations["backfill_broken_timestamp"] = raw_ts or "(blank)"
        approved_at = parse_ts(cell(row, approval_col))
        if approved_at is None:
            approved_at = call_connected_at
            if approved_at is not None:
                counts["approval_clock_fallbacks"] += 1
                annotations["backfill_approved_at_fallback"] = "timestamp_col"
        import_blocked = approved_at is None
        if import_blocked:
            counts["import_blocked_no_clock"] += 1
            annotations["backfill_import_blocked"] = "no_usable_clock"

        # ---- era / source (§3; sales 'migrated' + blank → manual) ----------
        source_raw = cell(row, "Source").lower()
        era = "ai" if source_raw == "ai" else "manual"
        if source_raw not in ("", "ai"):
            counts["source_migrated_mapped_manual"] += 1
            annotations["backfill_source_raw"] = source_raw
        db_source = "ai_reviewed" if era == "ai" else "manual"

        # ---- anomaly check vs v0_sheet (§4) ---------------------------------
        expected = v0_sheet_score(team_id, values)
        if expected is not None and abs(overall - expected) > 0.5 + 1e-9:
            counts["anomalies"] += 1
            annotations["backfill_anomaly"] = (
                f"overall_score inconsistent with v0_sheet formula "
                f"(sheet={overall:g}, recomputed={expected:.2f})"
            )

        # ---- per-section staged rows ----------------------------------------
        section_rows = []
        for i, (sec_id, kind) in enumerate(sections):
            value = values[sec_id]
            reasoning = cell(row, columns[6 + n + i]) or None
            confidence_raw = cell(row, columns[6 + 2 * n + i])
            confidence = CONFIDENCE_NORMALIZE.get(confidence_raw.lower()) if confidence_raw else None
            if confidence_raw and confidence != confidence_raw:
                counts["confidence_normalized_cells"] += 1
            is_ai_scored = era == "ai" and reasoning is not None and kind in ("numeric", "yn")
            section_rows.append({
                "section_id": sec_id,
                "kind": kind,
                "era": era,
                "value": value,
                "reasoning": reasoning if era == "ai" else None,  # §7: never fabricate
                "confidence": confidence if era == "ai" else None,
                "score_source": "ai" if is_ai_scored else "manual",
            })

        staged.append({
            "_key_basis": natural_key_basis(
                team_id, link, raw_ts, agent,
                cell(row, approval_col), overall_raw),
            "line": line_no,
            "team_id": team_id,
            "agent_name_raw": agent,
            "agent_email": cell(row, "Agent Email") or None,
            "evaluator_email": cell(row, "Evaluator Email") or None,
            "dialpad_link": link or None,
            "call_connected_at": call_connected_at,
            "approved_at": approved_at,
            "overall_score": overall,
            "state": "finalized",
            "source": db_source,
            "era": era,
            "models_used": MODELS_USED[era],
            "formula_version": TEAM_FORMULA_VERSION[team_id],
            "rubric_version": TEAM_RUBRIC_VERSION[team_id],
            "key_strengths": cell(row, "Key Strengths") or None,
            "opportunities": cell(row, "Opportunities") or None,
            "call_summary": cell(row, "Call Summary") or None,
            "caller_name": cell(row, "Caller Name") or None,
            "caller_phone": cell(row, "Caller Phone") or None,
            "annotations": annotations,
            "import_blocked": import_blocked,
            "sections": section_rows,
        })

    # ---- duplicate links (§8 D2): latest keeps the link ---------------------
    by_link: dict[str, list[dict]] = {}
    for s in staged:
        if s["dialpad_link"]:
            by_link.setdefault(s["dialpad_link"], []).append(s)
    for link, group in by_link.items():
        if len(group) < 2:
            continue
        counts["duplicate_link_groups"] += 1
        group.sort(key=lambda s: s["approved_at"] or s["call_connected_at"] or "")
        for older in group[:-1]:
            older["annotations"]["superseded_dialpad_link"] = older["dialpad_link"]
            older["dialpad_link"] = None
            counts["superseded_links"] += 1

    finalize_natural_keys(staged)
    keys = [s["natural_key"] for s in staged]
    if len(keys) != len(set(keys)):
        raise SystemExit("structural: natural-key collision in staging output")

    counts["rows_staged"] = len(staged)
    counts["rows_excluded"] = len(excluded)
    return {"staged": staged, "excluded": excluded, "counts": counts}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--team-id", required=True, choices=sorted(TEAM_SECTIONS))
    ap.add_argument("--csv", default=None)
    ap.add_argument("--out-dir", default=str(_DEFAULT_OUT))
    args = ap.parse_args()

    csv_path = Path(args.csv) if args.csv else (
        _REPO_ROOT / "database" / f"analyst_history_{args.team_id}.csv")
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    with csv_path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.reader(fh)
        columns = next(reader)
        rows = [dict(zip(columns, (r + [""] * len(columns))[: len(columns)]))
                for r in reader]

    result = stage_rows(args.team_id, rows, columns)
    staged, excluded, counts = result["staged"], result["excluded"], result["counts"]

    staging_path = out_dir / f"staging_{args.team_id}.jsonl"
    with staging_path.open("w", encoding="utf-8") as fh:
        for s in staged:
            fh.write(json.dumps(s, ensure_ascii=False, sort_keys=True) + "\n")

    exclusions_path = out_dir / f"backfill_exclusions_{args.team_id}.csv"
    with exclusions_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["line", "agent", "dialpad_link", "reason"])
        writer.writeheader()
        writer.writerows(excluded)

    # ---- expectation gates (§1/§2a inventory) -------------------------------
    exp = TEAM_EXPECTATIONS[args.team_id]
    gates = {
        "rows_in_matches_plan": counts["rows_in"] == exp["rows"],
        "excluded_matches_plan": counts["rows_excluded"] == exp["excluded"],
        "anomalies_match_plan": counts["anomalies"] == exp["anomalies"],
    }
    report = {
        "stage": "B0", "team_id": args.team_id, "csv": str(csv_path),
        "counts": counts, "expected": exp, "gates": gates,
        "outputs": {"staging": str(staging_path), "exclusions": str(exclusions_path)},
        "exclusion_reasons": sorted({e["reason"].split(":")[0] for e in excluded}),
    }
    report_path = out_dir / f"report_{args.team_id}_b0.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"[B0:{args.team_id}] in={counts['rows_in']} staged={counts['rows_staged']} "
          f"excluded={counts['rows_excluded']} anomalies={counts['anomalies']}")
    print(f"  binary cells normalized: {counts['binary_normalized_cells']}, "
          f"confidence normalized: {counts['confidence_normalized_cells']}")
    print(f"  broken timestamps: {counts['broken_timestamps']} (B2 repair queue), "
          f"approval fallbacks: {counts['approval_clock_fallbacks']}, "
          f"import-blocked (no clock): {counts['import_blocked_no_clock']}")
    print(f"  dup-link groups: {counts['duplicate_link_groups']} "
          f"({counts['superseded_links']} superseded)")
    for gate, ok in gates.items():
        print(f"  gate {gate}: {'OK' if ok else 'DRIFTED — review before B1'}")
    print(f"  report: {report_path}")

    return 0 if all(gates.values()) else 2


if __name__ == "__main__":
    sys.exit(main())

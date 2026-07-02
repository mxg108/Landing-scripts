#!/usr/bin/env python3
"""Extract golden score fixtures from a raw Analyst_History export.

Wave 2 Phase 2c (BackfillPlan.md §6). Reads the gitignored seed CSV, runs the
archived legacy formula over EVERY row as a parity check, then writes a
stratified, anonymized sample to tests/fixtures/overall_formula/<team>.json.

The fixtures carry only section answers + the sheet-computed overall — no
agent/caller names, links, or timestamps (era + year-month only).

Usage:
    cd qa-automation/AI-Scoring
    python scripts/extract_golden_fixtures.py [--team member_support] [--sample 40]

Exclusions mirror BackfillPlan.md §4/§8: all-zero test rows and manual
hard-zero overrides (D5) are dropped; hand-edited rows (sheet overall
inconsistent with the reconstructed formula) are KEPT and marked
expect_exact=false — they document the anomaly.

Sales note: the loader is column-shape-generic, but the section map below is
per-team; add a SALES block when its export lands (same CSV shape per
BackfillPlan.md).
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pandas as pd

_AI_SCORING = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_AI_SCORING))

from backend.models.formula import Formula  # noqa: E402
from backend.services.rule_engine import evaluate_formula  # noqa: E402

_REPO_ROOT = _AI_SCORING.parent.parent

# CSV column -> section_id, using the archived member_support_v1 rubric ids
# (migration 010 seed) — the v0_sheet formula keys match these so the
# §3.19.3 cross-check holds for backfilled rows. NOTE: two differ from the
# sheet-era history_ids (identity_validation, efficiency).
MS_SECTIONS = {
    "Greeting": "greeting",
    "Caller Identity Validation": "caller_identity_validation",
    "Purpose of the Call": "purpose_of_call",
    "Matching the Moment": "matching_the_moment",
    "Process Adherence": "process_adherence",
    "Call Resolution": "call_resolution",
    "Communication": "communication",
    "Efficiency & Call Handling": "efficiency_call_handling",
    "Documentation": "documentation",
    "Customer Resolution Indicator": "customer_resolution_indicator",
}
MS_BINARY = {"caller_identity_validation", "customer_resolution_indicator"}

BINARY_VOCAB = {"Y": "Y", "Yes": "Y", "N": "N", "No": "N", "Not Applicable": "NA", "NA": "NA"}

TEAMS = {
    "member_support": {
        "csv": _REPO_ROOT / "database" / "analyst_history_member_support.csv",
        "formula": _AI_SCORING / "backend" / "config" / "scoring" / "member_support" / "v0_sheet.json",
        "sections": MS_SECTIONS,
        "binary": MS_BINARY,
    },
}


def load_rows(cfg) -> list[dict]:
    df = pd.read_csv(cfg["csv"])
    df.columns = [c.strip() for c in df.columns]
    rows = []
    for idx, r in df.iterrows():
        answers: dict = {}
        ok = True
        for col, sid in cfg["sections"].items():
            raw = str(r[col]).strip()
            if sid in cfg["binary"]:
                val = BINARY_VOCAB.get(raw)
                if val is None:
                    ok = False
                    break
                answers[sid] = val
            else:
                if not raw.isdigit() or not (1 <= int(raw) <= 5):
                    ok = False  # '0' rows are the test artifacts
                    break
                answers[sid] = int(raw)
        if not ok:
            continue
        ts = pd.to_datetime(r["Timestamp"], errors="coerce")
        rows.append({
            "row": int(idx) + 2,  # 1-based + header, for traceability against the sheet
            "answers": answers,
            "sheet_overall": int(r["Overall Score"]),
            "era": "ai" if str(r.get("Source", "")).strip() == "ai" else "manual",
            "year_month": ts.strftime("%Y-%m") if pd.notna(ts) else None,
        })
    return rows


def engine_score(formula: Formula, answers: dict) -> int:
    result = evaluate_formula(formula, answers)
    return int(Decimal(str(result.final_score)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--team", default="member_support", choices=sorted(TEAMS))
    ap.add_argument("--sample", type=int, default=40)
    args = ap.parse_args()
    cfg = TEAMS[args.team]

    formula = Formula.model_validate(json.loads(cfg["formula"].read_text()))
    rows = load_rows(cfg)
    print(f"loaded {len(rows)} scoreable rows (test artifacts already dropped)")

    matched, hard_zero, hand_edit = [], [], []
    for row in rows:
        row["engine_overall"] = engine_score(formula, row["answers"])
        delta = abs(row["engine_overall"] - row["sheet_overall"])
        if delta == 0:
            matched.append(row)
        elif row["sheet_overall"] == 0:
            hard_zero.append(row)  # D5: manual hard-zero override — excluded
        else:
            hand_edit.append(row)

    total = len(rows)
    print(f"exact parity under {formula.formula_id}: {len(matched)}/{total} "
          f"({len(matched) / total:.1%}) | hard-zero excluded: {len(hard_zero)} "
          f"| hand-edits kept as expected-mismatch: {len(hand_edit)}")

    # Stratified sample: era x score band x NA presence, deterministic.
    rng = random.Random(42)
    strata: dict = {}
    for row in matched:
        has_na = any(v == "NA" for v in row["answers"].values())
        key = (row["era"], row["sheet_overall"] // 20, has_na)
        strata.setdefault(key, []).append(row)
    sample: list = []
    keys = sorted(strata, key=str)
    while len(sample) < min(args.sample, len(matched)) and keys:
        for key in list(keys):
            pool = strata[key]
            if not pool:
                keys.remove(key)
                continue
            sample.append(pool.pop(rng.randrange(len(pool))))
            if len(sample) >= min(args.sample, len(matched)):
                break

    fixtures = []
    for n, row in enumerate(sorted(sample, key=lambda r: r["row"]), 1):
        fixtures.append({
            "label": f"{args.team[:2]}-{n:04d}",
            "era": row["era"],
            "year_month": row["year_month"],
            "answers": row["answers"],
            "sheet_overall": row["sheet_overall"],
            "expect_exact": True,
        })
    for n, row in enumerate(sorted(hand_edit, key=lambda r: r["row"]), 1):
        fixtures.append({
            "label": f"{args.team[:2]}-edit-{n:02d}",
            "era": row["era"],
            "year_month": row["year_month"],
            "answers": row["answers"],
            "sheet_overall": row["sheet_overall"],
            "engine_overall": row["engine_overall"],
            "expect_exact": False,
            "note": "sheet overall hand-edited — inconsistent with the reconstructed formula (BackfillPlan.md §4)",
        })

    out = _AI_SCORING / "tests" / "fixtures" / "overall_formula" / f"{args.team}.json"
    out.write_text(json.dumps({
        "formula_id": formula.formula_id,
        "source": "Analyst_History export (BackfillPlan.md §6); anonymized",
        "fixtures": fixtures,
    }, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(fixtures)} fixtures -> {out.relative_to(_AI_SCORING)}")

    na_count = sum(1 for f in fixtures if any(v == "NA" for v in f["answers"].values()))
    print(f"sample spread: {sum(1 for f in fixtures if f['era'] == 'ai')} ai / "
          f"{sum(1 for f in fixtures if f['era'] == 'manual')} manual | {na_count} with NA")


if __name__ == "__main__":
    main()

"""Phase E — Member Support Analyst_History migration.

Reads the legacy MS Analyst_History tab (after the user has manually
renamed it to ``Analyst_History_legacy``) and writes a fresh
``Analyst_History`` tab in the new HistoryLayout(N=10) shape (42 cols
A–AP).

Source layout reference:
    qa-automation/teams/member_support/Config.js (pre-Phase-2 hand-coded
    HISTORY_COL) — see also PhaseTwo §Migration §"MS Analyst_History
    (one-shot, in-place reorder)" for the per-column mapping table.

Pipeline (dry-run by default):
    1. Open MS spreadsheet via env-auth.
    2. Read source tab (Analyst_History_legacy) — one API call.
    3. Skip header row.
    4. Transform each row into the new layout in memory.
    5. Print summary + a transformed-row sample.
    6. If --apply: prompt for confirmation, create Analyst_History_PENDING,
       batch-write, run verification gates, rename to Analyst_History.

Usage:
    # Dry-run (no writes, prints what would happen):
    python qa-automation/scripts/migrate_ms_history.py

    # Commit:
    python qa-automation/scripts/migrate_ms_history.py --apply

    # Limit to first N rows for a small live test:
    python qa-automation/scripts/migrate_ms_history.py --apply --limit 5

    # Re-run after a previous --apply got stuck (deletes the _PENDING tab):
    python qa-automation/scripts/migrate_ms_history.py --apply --force
"""

from __future__ import annotations

import argparse
import random
import sys

import migration_utils as mu

TEAM_ID = "member_support"


# ---------------------------------------------------------------------------
# Per-column mapping — drives the row transform.
# Source (PhaseTwo §Migration → "MS Analyst_History" table)
# ---------------------------------------------------------------------------

# Prefix fields (legacy_col, new_col, label)
_PREFIX_MAP: list[tuple[int, int, str]] = [
    (0, 0, "agent_name"),
    (1, 1, "agent_email"),
    (2, 2, "timestamp"),
    (14, 3, "evaluator_email"),       # was manager_email
    (15, 4, "dialpad_link"),
    (3, 5, "overall_score"),
]

# Section mappings: per layout_idx (0..9), the legacy (score_col, reason_col, conf_col)
# Order = sections_by_number(member_support).
_SECTION_MAP: list[tuple[int, int, int, int, str]] = [
    # (layout_idx, legacy_score, legacy_reason, legacy_conf, label)
    (0,  4, 20, 19, "greeting"),
    (1, 12, 22, 21, "caller_identity_validation"),
    (2,  5, 24, 23, "purpose_of_call"),
    (3,  6, 26, 25, "matching_the_moment"),
    (4,  7, 28, 27, "process_adherence"),
    (5,  8, 30, 29, "call_resolution"),
    (6,  9, 32, 31, "communication"),
    (7, 10, 34, 33, "efficiency_call_handling"),
    (8, 11, 41, 40, "documentation"),
    (9, 13, 36, 35, "customer_resolution_indicator"),
]

# Trailing fields (legacy_col, new_col, label)
_TRAILING_MAP: list[tuple[int, int, str]] = [
    (16, 36, "key_strengths"),
    (17, 37, "opportunities"),        # was improvements
    (37, 38, "call_summary"),
    (38, 39, "caller_name"),
    (39, 40, "caller_phone"),
    (18, 41, "source"),
]

EXPECTED_LEGACY_WIDTH = 42  # cols A-AP on the old AH


def _cell(row: list[str], idx: int) -> str:
    """Safe legacy-row access — returns '' if the row is shorter than idx."""
    return row[idx] if idx < len(row) else ""


def transform_row(legacy_row: list[str], layout) -> list[str]:
    """Build the new-layout row from a legacy row.

    The new layout has 42 cells (same total width as legacy for MS), but
    the *order* changes per the mapping tables above.
    """
    new_row = mu.blank_history_row(layout)

    for src, dst, _ in _PREFIX_MAP:
        new_row[dst] = _cell(legacy_row, src)

    for idx, score_src, reason_src, conf_src, _ in _SECTION_MAP:
        new_row[layout.col_score(idx)]      = _cell(legacy_row, score_src)
        new_row[layout.col_reasoning(idx)]  = _cell(legacy_row, reason_src)
        new_row[layout.col_confidence(idx)] = _cell(legacy_row, conf_src)

    for src, dst, _ in _TRAILING_MAP:
        new_row[dst] = _cell(legacy_row, src)

    return new_row


# ---------------------------------------------------------------------------
# Verification gates (PhaseTwo §Migration §MS verification)
# ---------------------------------------------------------------------------

def verify(
    legacy_rows: list[list[str]],
    new_rows: list[list[str]],
    layout,
    sample_size: int = 10,
    pair_sample_size: int = 5,
) -> mu.VerificationReport:
    """Run the three MS verification gates against the in-memory rows."""
    report = mu.VerificationReport()

    # 1) Row counts match
    report.add(
        name="row counts match",
        passed=len(legacy_rows) == len(new_rows),
        detail=f"{len(legacy_rows)} legacy vs {len(new_rows)} new",
    )

    if not legacy_rows or not new_rows:
        return report

    # 2) Sample N rows: overall_score + each section's score match
    rng = random.Random(42)
    indices = rng.sample(
        range(len(legacy_rows)), min(sample_size, len(legacy_rows))
    )

    score_mismatches: list[str] = []
    for i in indices:
        leg = legacy_rows[i]
        new = new_rows[i]
        # overall_score: legacy col 3 -> new col 5
        if _cell(leg, 3) != new[5]:
            score_mismatches.append(
                f"row {i}: overall_score legacy[3]={_cell(leg, 3)!r} "
                f"vs new[5]={new[5]!r}"
            )
        # each section
        for idx, score_src, _, _, label in _SECTION_MAP:
            if _cell(leg, score_src) != new[layout.col_score(idx)]:
                score_mismatches.append(
                    f"row {i}: section {label} legacy[{score_src}]={_cell(leg, score_src)!r} "
                    f"vs new[{layout.col_score(idx)}]={new[layout.col_score(idx)]!r}"
                )
    report.add(
        name=f"sample {len(indices)} rows: scores match",
        passed=not score_mismatches,
        detail="all match" if not score_mismatches else f"{len(score_mismatches)} mismatch(es); first: {score_mismatches[0]}",
    )

    # 3) Reasoning/confidence pair re-pairing
    pair_indices = rng.sample(
        range(len(legacy_rows)), min(pair_sample_size, len(legacy_rows))
    )
    pair_mismatches: list[str] = []
    for i in pair_indices:
        leg = legacy_rows[i]
        new = new_rows[i]
        for idx, _, reason_src, conf_src, label in _SECTION_MAP:
            if _cell(leg, reason_src) != new[layout.col_reasoning(idx)]:
                pair_mismatches.append(
                    f"row {i}: {label} reason legacy[{reason_src}] vs new[{layout.col_reasoning(idx)}]"
                )
            if _cell(leg, conf_src) != new[layout.col_confidence(idx)]:
                pair_mismatches.append(
                    f"row {i}: {label} confidence legacy[{conf_src}] vs new[{layout.col_confidence(idx)}]"
                )
    report.add(
        name=f"sample {len(pair_indices)} rows: reasoning/confidence pairs match",
        passed=not pair_mismatches,
        detail="all match" if not pair_mismatches else f"{len(pair_mismatches)} mismatch(es); first: {pair_mismatches[0]}",
    )

    return report


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="MS Analyst_History migration to the Phase-2 derived layout.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually write to the spreadsheet (default: dry run only).",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Delete and recreate _PENDING tab if it already exists.",
    )
    parser.add_argument(
        "--limit", type=int, default=0,
        help="Process only the first N data rows (handy for testing).",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Print every transformed row to stdout (noisy).",
    )
    args = parser.parse_args()

    # 1) Load config + connect ------------------------------------------------
    cfg, layout = mu.history_layout_for(TEAM_ID)
    legacy_tab = cfg.sheets.analyst_history.tab_name_legacy or "Analyst_History_legacy"
    final_tab = cfg.sheets.analyst_history.tab_name
    pending_tab = final_tab + mu.PENDING_SUFFIX

    mu.print_banner(
        title="MS Analyst_History migration",
        fields={
            "Team":         TEAM_ID,
            "Legacy tab":   legacy_tab,
            "Pending tab":  pending_tab,
            "Final tab":    final_tab,
            "Layout":       f"N={layout.n}, total_width={layout.total_width} (A–{mu.column_letter_range(layout)})",
            "Mode":         "APPLY (writes will execute)" if args.apply else "DRY RUN",
        },
        dry_run=not args.apply,
    )

    ss = mu.open_team_spreadsheet(TEAM_ID)

    legacy_ws = mu.get_worksheet(ss, legacy_tab)
    if legacy_ws is None:
        print(f"ERROR: legacy tab '{legacy_tab}' not found in the MS spreadsheet.")
        print(f"       Rename the current 'Analyst_History' tab to '{legacy_tab}'")
        print(f"       in the Google Sheet before re-running this script.")
        return 2

    # 2) Read source ---------------------------------------------------------
    print(f"  Reading '{legacy_tab}'...")
    all_legacy = legacy_ws.get_all_values()
    if not all_legacy:
        print(f"ERROR: '{legacy_tab}' is empty.")
        return 2

    header, legacy_data = all_legacy[0], all_legacy[1:]
    print(f"    -> {len(all_legacy)} rows total (1 header + {len(legacy_data)} data)")
    print(f"    -> {len(header)} columns in header"
          + ("" if len(header) >= EXPECTED_LEGACY_WIDTH
             else f" (expected >= {EXPECTED_LEGACY_WIDTH})"))

    if args.limit > 0 and args.limit < len(legacy_data):
        print(f"    -> --limit {args.limit}: processing first {args.limit} rows only")
        legacy_data = legacy_data[: args.limit]

    # 3) Transform -----------------------------------------------------------
    print(f"  Transforming {len(legacy_data)} rows in memory...")
    new_rows = [transform_row(r, layout) for r in legacy_data]

    if args.verbose:
        for i, (leg, new) in enumerate(zip(legacy_data[:3], new_rows[:3])):
            print(f"    row {i} legacy: {leg[:10]}... (len {len(leg)})")
            print(f"    row {i} new:    {new[:10]}... (len {len(new)})")

    # Show a sample
    if new_rows:
        sample = new_rows[0]
        print(f"  Sample new row (first 6 + first 3 scores):")
        print(f"    [agent_name, agent_email, timestamp, evaluator_email, dialpad_link, overall_score] =")
        print(f"      {sample[:6]}")
        print(f"    [score[0..2]] = {sample[layout.scores_start:layout.scores_start + 3]}")

    # 4) Run verification gates against the in-memory transformation --------
    report = verify(legacy_data, new_rows, layout)
    report.print_summary()

    if not report.all_passed:
        print()
        print("ERROR: in-memory verification failed. Not writing anything.")
        return 3

    # 5) Dry-run stops here --------------------------------------------------
    if not args.apply:
        print()
        print(f"  Dry run complete. Would write {len(new_rows)} rows to '{pending_tab}'")
        print(f"  then rename '{pending_tab}' -> '{final_tab}'.")
        print(f"  Re-run with --apply to commit.")
        return 0

    # 6) Apply: PENDING tab + write + verify + rename -----------------------
    if not mu.confirm_apply():
        print("  Aborted by user.")
        return 1

    mu.abort_if_tab_exists(ss, pending_tab, force=args.force)
    mu.abort_if_tab_exists(ss, final_tab, force=args.force)

    print(f"  Creating tab '{pending_tab}' "
          f"(rows={len(new_rows) + 1}, cols={layout.total_width})...")
    pending_ws = mu.create_target_tab(
        ss, pending_tab,
        n_rows=len(new_rows) + 1,  # +1 for header
        n_cols=layout.total_width,
    )

    # Write the header in canonical order, using `sec.name` (display label)
    # for section columns to match the convention in import_sales_history.py.
    print(f"  Writing header (canonical layout + display-name labels)...")
    new_header = mu.blank_history_row(layout)
    new_header[0] = "Agent Name"
    new_header[1] = "Agent Email"
    new_header[2] = "Timestamp"
    new_header[3] = "Evaluator Email"
    new_header[4] = "Dialpad Link"
    new_header[5] = "Overall Score"
    for i, sec in enumerate(cfg.sections_by_number):
        new_header[layout.col_score(i)]      = sec.name
        new_header[layout.col_reasoning(i)]  = f"{sec.name} reasoning"
        new_header[layout.col_confidence(i)] = f"{sec.name} confidence"
    new_header[layout.col_key_strengths] = "Key Strengths"
    new_header[layout.col_opportunities] = "Opportunities"
    new_header[layout.col_call_summary]  = "Call Summary"
    new_header[layout.col_caller_name]   = "Caller Name"
    new_header[layout.col_caller_phone]  = "Caller Phone"
    new_header[layout.col_source]        = "Source"
    pending_ws.update("A1", [new_header], value_input_option="USER_ENTERED")

    print(f"  Writing {len(new_rows)} data rows to '{pending_tab}'...")
    written = mu.chunked_append_rows(pending_ws, new_rows, verbose=args.verbose)
    print(f"    -> wrote {written}/{len(new_rows)} rows.")

    # 7) Post-write read-back gate ------------------------------------------
    print(f"  Reading back '{pending_tab}' for post-write verification...")
    after = pending_ws.get_all_values()
    after_data = after[1:] if after else []
    written_count_ok = len(after_data) == len(new_rows)
    print(f"    -> read back {len(after_data)} data rows "
          f"({'matches expected' if written_count_ok else 'MISMATCH'})")

    if not written_count_ok:
        print()
        print("ERROR: post-write row count mismatch. Leaving _PENDING tab in place")
        print("       for inspection; not renaming. Investigate, then either")
        print(f"       delete '{pending_tab}' manually or re-run with --force.")
        return 4

    # 8) Rename PENDING -> final --------------------------------------------
    print(f"  Renaming '{pending_tab}' -> '{final_tab}'...")
    mu.rename_worksheet(pending_ws, final_tab)
    print(f"    -> done.")
    print()
    print(f"  Migration complete. '{final_tab}' now holds {len(new_rows)} migrated rows.")
    print(f"  Legacy data remains at '{legacy_tab}' for rollback.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

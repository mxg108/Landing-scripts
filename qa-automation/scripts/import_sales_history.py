"""Phase E — Sales 243-row history import from Form Responses 3.

Reads the Sales spreadsheet's ``Form Responses 3`` tab (the legacy form-
submission record) and writes a fresh ``Analyst_History`` tab in the
HistoryLayout(N=19) shape (69 cols A–BQ) for the Sales team.

Source layout reference:
    PhaseTwo §Migration §"Sales — 243-row import from FR3" — both the
    section-index alignment table (FR3 D-V → score idx 0-18, already in
    section_number order) and the non-section mapping table.

Pipeline (dry-run by default):
    1. Open Sales spreadsheet via env-auth.
    2. Read ``Form Responses 3`` — one API call.
    3. Read Sales Mails tab — one API call (for agent_email lookup).
    4. Identify rows needing date backfill (FR3 col B blank).
    5. For those, fetch Dialpad call-details (cached on disk; 5 req/sec).
    6. Transform each row to the new layout. Log Mails-resolution rate.
    7. Print summary.
    8. If --apply: prompt, create Analyst_History_PENDING, batch-write,
       run verification gates, rename to Analyst_History.

Usage:
    # Dry-run (no writes, no Dialpad calls):
    python qa-automation/scripts/import_sales_history.py

    # Dry-run with Dialpad backfill performed (so cache is warm before --apply):
    python qa-automation/scripts/import_sales_history.py --backfill

    # Commit:
    python qa-automation/scripts/import_sales_history.py --apply --backfill

    # Skip Dialpad entirely; use FR3 col Y as fallback if B blank:
    python qa-automation/scripts/import_sales_history.py --apply --no-backfill
"""

from __future__ import annotations

import argparse
import random
import sys

import migration_utils as mu

TEAM_ID = "sales"

# Source FR3 column indices (0-based)
COL_DIALPAD_LINK   = 0   # A
COL_CALL_DATE      = 1   # B
COL_AGENT_NAME     = 2   # C
SCORES_COL_START   = 3   # D  (first section score column)
SCORES_COL_END     = 22  # exclusive — covers D-V (19 cols)
COL_EVALUATOR      = 22  # W
COL_FEEDBACK       = 23  # X  (combined "what went well + opportunities")
COL_SUBMITTED_TS   = 24  # Y  (form-submission timestamp; fallback for B)

FR3_TAB = "Form Responses 3"

# PhaseTwo describes a "243-row import" but FR3 drifts over time as
# duplicates get cleaned up by analysts. The gate is a sanity check
# (catches "wrong sheet" / "lost half the data"), not an exact match.
EXPECTED_ROW_COUNT_APPROX = 243
ROW_COUNT_TOLERANCE = 20      # 223–263 passes; anything outside fails the gate

MIN_MAILS_RESOLUTION_RATE = 0.70


# ---------------------------------------------------------------------------
# Mails lookup (Sales sheet's Mails tab)
# ---------------------------------------------------------------------------

def build_mails_index(mails_rows: list[list[str]]) -> dict[str, str]:
    """Map normalized agent name -> email from the Mails tab.

    Header row skipped. Names are lowercased + stripped for case-insensitive
    lookup (the QA pipeline does the same in sheets_service._lookup_agent_email).
    """
    index = {}
    for row in mails_rows[1:]:
        if len(row) < 2:
            continue
        name = (row[0] or "").strip().lower()
        email = (row[1] or "").strip()
        if name:
            index[name] = email
    return index


# ---------------------------------------------------------------------------
# Row transform
# ---------------------------------------------------------------------------

def transform_row(
    fr3_row: list[str],
    layout,
    mails_index: dict[str, str],
    backfilled_dates: dict[str, str],
    row_id: str,
) -> tuple[list[str], bool, str]:
    """Build the new-layout row from one FR3 row.

    Returns (new_row, mails_resolved, agent_name).
    ``mails_resolved`` is True iff the agent's email was found in Mails.
    """
    def cell(idx: int) -> str:
        return fr3_row[idx] if idx < len(fr3_row) else ""

    new_row = mu.blank_history_row(layout)

    dialpad_link = cell(COL_DIALPAD_LINK).strip()
    agent_name = cell(COL_AGENT_NAME).strip()
    evaluator_email = cell(COL_EVALUATOR).strip()
    feedback_combined = cell(COL_FEEDBACK).strip()

    # Timestamp: prefer FR3 col B (Call Date); fall back to backfilled
    # date_connected (from Dialpad) or FR3 col Y (form-submission timestamp).
    timestamp = cell(COL_CALL_DATE).strip()
    if not timestamp:
        timestamp = backfilled_dates.get(row_id, "") or cell(COL_SUBMITTED_TS).strip()

    # Mails lookup for agent email
    agent_email = mails_index.get(agent_name.lower(), "")
    mails_resolved = bool(agent_email)

    # Prefix fields
    new_row[0] = agent_name
    new_row[1] = agent_email
    new_row[2] = timestamp
    new_row[3] = evaluator_email
    new_row[4] = dialpad_link
    # overall_score (col 5) stays blank for migrated rows — analytics filter
    # on source = 'migrated' is the safety net.

    # Section scores: FR3 cols D-V map directly to layout score range
    # [scores_start, scores_end) in section_number order.
    for i, src_col in enumerate(range(SCORES_COL_START, SCORES_COL_END)):
        new_row[layout.col_score(i)] = cell(src_col)
        # Reasoning + confidence stay blank — no AI ran on migrated rows.

    # Trailing fields
    # key_strengths stays blank — combined feedback lands in opportunities only.
    new_row[layout.col_opportunities] = feedback_combined
    # call_summary, caller_name, caller_phone stay blank.
    new_row[layout.col_source] = "migrated"

    return new_row, mails_resolved, agent_name


# ---------------------------------------------------------------------------
# Verification gates (PhaseTwo §Migration §Sales verification)
# ---------------------------------------------------------------------------

def verify(
    fr3_data: list[list[str]],
    new_rows: list[list[str]],
    layout,
    mails_resolved_count: int,
    sample_size: int = 10,
) -> mu.VerificationReport:
    report = mu.VerificationReport()

    # 1) Row count is sane (within tolerance of the documented ~243).
    # Exact match isn't required — FR3 drifts as duplicates are cleaned up.
    drift = len(new_rows) - EXPECTED_ROW_COUNT_APPROX
    within_tolerance = abs(drift) <= ROW_COUNT_TOLERANCE
    if len(new_rows) == EXPECTED_ROW_COUNT_APPROX:
        detail = f"{len(new_rows)} rows (exact match)"
    elif within_tolerance:
        detail = (
            f"{len(new_rows)} rows (within ±{ROW_COUNT_TOLERANCE} of "
            f"~{EXPECTED_ROW_COUNT_APPROX}; drift {drift:+d})"
        )
    else:
        detail = (
            f"{len(new_rows)} rows — drift {drift:+d} exceeds tolerance "
            f"±{ROW_COUNT_TOLERANCE}; likely wrong source tab"
        )
    report.add(
        name=f"row count near ~{EXPECTED_ROW_COUNT_APPROX} (±{ROW_COUNT_TOLERANCE})",
        passed=within_tolerance and len(new_rows) > 0,
        detail=detail,
    )

    if not new_rows:
        return report

    # 2) Sample N rows: section scores at idx 0-18 match FR3 cols D-V
    rng = random.Random(42)
    indices = rng.sample(range(len(fr3_data)), min(sample_size, len(fr3_data)))
    score_mismatches: list[str] = []
    for i in indices:
        fr3 = fr3_data[i]
        new = new_rows[i]
        for j, src_col in enumerate(range(SCORES_COL_START, SCORES_COL_END)):
            fr3_val = fr3[src_col] if src_col < len(fr3) else ""
            new_val = new[layout.col_score(j)]
            if fr3_val != new_val:
                score_mismatches.append(
                    f"row {i}: section idx {j}: FR3[{src_col}]={fr3_val!r} "
                    f"vs new[{layout.col_score(j)}]={new_val!r}"
                )
    report.add(
        name=f"sample {len(indices)} rows: section scores match FR3 D-V",
        passed=not score_mismatches,
        detail="all match" if not score_mismatches
        else f"{len(score_mismatches)} mismatch(es); first: {score_mismatches[0]}",
    )

    # 3) source = 'migrated' on every row
    bad_source = [i for i, r in enumerate(new_rows)
                  if r[layout.col_source] != "migrated"]
    report.add(
        name="every row has source='migrated'",
        passed=not bad_source,
        detail=f"all {len(new_rows)} rows tagged"
        if not bad_source else f"{len(bad_source)} rows missing tag",
    )

    # 4) Mails resolution rate >= 70%
    rate = mails_resolved_count / len(new_rows) if new_rows else 0.0
    report.add(
        name=f"Mails resolution rate >= {MIN_MAILS_RESOLUTION_RATE:.0%}",
        passed=rate >= MIN_MAILS_RESOLUTION_RATE,
        detail=f"{mails_resolved_count}/{len(new_rows)} = {rate:.1%}",
    )

    return report


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sales FR3 → Analyst_History import (Phase 2 layout).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually write to the Sales spreadsheet (default: dry run only).",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Delete and recreate _PENDING / final tab if it already exists.",
    )
    parser.add_argument(
        "--backfill", action="store_true",
        help="Fetch missing dates from Dialpad (cached on disk). "
             "Mutually exclusive with --no-backfill.",
    )
    parser.add_argument(
        "--no-backfill", action="store_true",
        help="Skip Dialpad entirely; use FR3 col Y as the fallback for blank B.",
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

    if args.backfill and args.no_backfill:
        print("ERROR: --backfill and --no-backfill are mutually exclusive.")
        return 2

    # 1) Load config + open spreadsheet -------------------------------------
    cfg, layout = mu.history_layout_for(TEAM_ID)
    final_tab = cfg.sheets.analyst_history.tab_name
    pending_tab = final_tab + mu.PENDING_SUFFIX
    mails_tab = cfg.sheets.mails_tab

    mu.print_banner(
        title="Sales 243-row history import",
        fields={
            "Team":          TEAM_ID,
            "Source tab":    FR3_TAB,
            "Mails tab":     mails_tab,
            "Pending tab":   pending_tab,
            "Final tab":     final_tab,
            "Layout":        f"N={layout.n}, total_width={layout.total_width} "
                             f"(A–{mu.column_letter_range(layout)})",
            "Mode":          "APPLY (writes will execute)" if args.apply else "DRY RUN",
            "Backfill":      ("on" if args.backfill else
                              ("off" if args.no_backfill else "off (use --backfill to enable)")),
        },
        dry_run=not args.apply,
    )

    ss = mu.open_team_spreadsheet(TEAM_ID)

    fr3_ws = mu.get_worksheet(ss, FR3_TAB)
    if fr3_ws is None:
        print(f"ERROR: '{FR3_TAB}' tab not found on the Sales spreadsheet.")
        return 2

    mails_ws = mu.get_worksheet(ss, mails_tab)
    if mails_ws is None:
        print(f"ERROR: '{mails_tab}' tab not found on the Sales spreadsheet.")
        return 2

    # 2) Read source + Mails ------------------------------------------------
    print(f"  Reading '{FR3_TAB}'...")
    fr3_all = fr3_ws.get_all_values()
    if not fr3_all:
        print(f"ERROR: '{FR3_TAB}' is empty.")
        return 2
    fr3_header, fr3_data = fr3_all[0], fr3_all[1:]
    print(f"    -> {len(fr3_all)} rows total (1 header + {len(fr3_data)} data)")
    if len(fr3_data) != EXPECTED_ROW_COUNT_APPROX:
        drift = len(fr3_data) - EXPECTED_ROW_COUNT_APPROX
        within = abs(drift) <= ROW_COUNT_TOLERANCE
        print(f"    -> NOTE: ~{EXPECTED_ROW_COUNT_APPROX} expected, got {len(fr3_data)} "
              f"(drift {drift:+d}, {'within' if within else 'OUTSIDE'} ±{ROW_COUNT_TOLERANCE} tolerance)")

    if args.limit > 0 and args.limit < len(fr3_data):
        print(f"    -> --limit {args.limit}: processing first {args.limit} rows only")
        fr3_data = fr3_data[: args.limit]

    print(f"  Reading '{mails_tab}'...")
    mails_rows = mails_ws.get_all_values()
    mails_index = build_mails_index(mails_rows)
    print(f"    -> {len(mails_index)} entries in the Sales Mails roster")

    # 3) Date backfill ------------------------------------------------------
    backfilled: dict[str, str] = {}
    if args.backfill:
        needed: list[tuple[str, str]] = []
        for i, row in enumerate(fr3_data):
            if not (row[COL_CALL_DATE] if COL_CALL_DATE < len(row) else "").strip():
                link = row[COL_DIALPAD_LINK] if COL_DIALPAD_LINK < len(row) else ""
                needed.append((f"r{i}", link))
        if needed:
            print(f"  Backfilling dates for {len(needed)} rows via Dialpad "
                  f"(cache: {mu.DIALPAD_CACHE_DIR}; rate-limited)...")
            backfilled = mu.backfill_dates(needed)
            resolved = sum(1 for v in backfilled.values() if v)
            print(f"    -> resolved {resolved}/{len(needed)} dates from Dialpad")
        else:
            print(f"  No rows need date backfill — all FR3 col B cells are populated.")

    # 4) Transform ----------------------------------------------------------
    print(f"  Transforming {len(fr3_data)} rows in memory...")
    new_rows: list[list[str]] = []
    mails_resolved_count = 0
    unresolved_names: set[str] = set()
    for i, fr3_row in enumerate(fr3_data):
        row_id = f"r{i}"
        new_row, resolved, agent_name = transform_row(
            fr3_row, layout, mails_index, backfilled, row_id,
        )
        new_rows.append(new_row)
        if resolved:
            mails_resolved_count += 1
        elif agent_name:
            unresolved_names.add(agent_name)

    print(f"    -> Mails resolution: {mails_resolved_count}/{len(new_rows)} "
          f"({(mails_resolved_count / max(len(new_rows), 1)):.1%})")
    if unresolved_names:
        # Surface a handful of unresolved names so the user can update Mails.
        sample_names = sorted(unresolved_names)[:10]
        print(f"    -> {len(unresolved_names)} unique unresolved agent name(s); "
              f"first {len(sample_names)}:")
        for n in sample_names:
            print(f"         - {n}")

    if args.verbose:
        for i, new in enumerate(new_rows[:2]):
            print(f"    sample row {i}: {new[:8]}... (len {len(new)})")

    # 5) Verification gates against in-memory transform --------------------
    report = verify(fr3_data, new_rows, layout, mails_resolved_count)
    report.print_summary()

    if not report.all_passed:
        print()
        print("ERROR: verification gates failed. Not writing anything.")
        return 3

    # 6) Dry-run stops here -------------------------------------------------
    if not args.apply:
        print()
        print(f"  Dry run complete. Would write {len(new_rows)} rows to '{pending_tab}'")
        print(f"  then rename '{pending_tab}' -> '{final_tab}'.")
        print(f"  Re-run with --apply to commit.")
        return 0

    # 7) Apply: PENDING + write + verify + rename --------------------------
    if not mu.confirm_apply():
        print("  Aborted by user.")
        return 1

    mu.abort_if_tab_exists(ss, pending_tab, force=args.force)
    mu.abort_if_tab_exists(ss, final_tab, force=args.force)

    print(f"  Creating '{pending_tab}' (rows={len(new_rows) + 1}, cols={layout.total_width})...")
    pending_ws = mu.create_target_tab(
        ss, pending_tab,
        n_rows=len(new_rows) + 1,
        n_cols=layout.total_width,
    )

    # Header derived from the layout structure + section labels.
    print(f"  Writing header (derived from layout + section labels)...")
    sections = cfg.sections_by_number
    new_header = mu.blank_history_row(layout)
    new_header[0] = "Agent Name"
    new_header[1] = "Agent Email"
    new_header[2] = "Timestamp"
    new_header[3] = "Evaluator Email"
    new_header[4] = "Dialpad Link"
    new_header[5] = "Overall Score"
    for i, sec in enumerate(sections):
        new_header[layout.col_score(i)] = sec.name
        new_header[layout.col_reasoning(i)] = f"{sec.name} reasoning"
        new_header[layout.col_confidence(i)] = f"{sec.name} confidence"
    new_header[layout.col_key_strengths] = "Key Strengths"
    new_header[layout.col_opportunities] = "Opportunities"
    new_header[layout.col_call_summary]  = "Call Summary"
    new_header[layout.col_caller_name]   = "Caller Name"
    new_header[layout.col_caller_phone]  = "Caller Phone"
    new_header[layout.col_source]        = "Source"
    pending_ws.update("A1", [new_header], value_input_option="USER_ENTERED")

    print(f"  Writing {len(new_rows)} data rows...")
    written = mu.chunked_append_rows(pending_ws, new_rows, verbose=args.verbose)
    print(f"    -> wrote {written}/{len(new_rows)} rows.")

    # 8) Post-write read-back ----------------------------------------------
    print(f"  Reading back '{pending_tab}' for post-write verification...")
    after = pending_ws.get_all_values()
    after_data = after[1:] if after else []
    if len(after_data) != len(new_rows):
        print()
        print(f"ERROR: post-write row count mismatch "
              f"({len(after_data)} read vs {len(new_rows)} written).")
        print(f"       Leaving '{pending_tab}' in place; not renaming.")
        return 4
    print(f"    -> read back {len(after_data)} rows. Match.")

    # 9) Rename ------------------------------------------------------------
    print(f"  Renaming '{pending_tab}' -> '{final_tab}'...")
    mu.rename_worksheet(pending_ws, final_tab)
    print(f"    -> done.")
    print()
    print(f"  Sales import complete. '{final_tab}' now holds {len(new_rows)} migrated rows")
    print(f"  (all tagged source='migrated' for analytics filtering).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

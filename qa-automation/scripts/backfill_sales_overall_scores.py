"""Phase E follow-up — back-fill `overall_score` on Sales Analyst_History.

The Sales 243-row import (`import_sales_history.py`) deliberately left
column F (`overall_score`) blank because the canonical overall score
lives on the Sales `Scores` tab in column Y — computed by an
ARRAYFORMULA over the weighted section scores.

This script copies `Scores!Y` into `Analyst_History!F` for each row that
matches on (agent_name, date).

Matching strategy:
    Scores!C (Agent) ↔ Analyst_History!A (agent_name)
        — case-insensitive, whitespace-stripped equality
    Scores!B (Date)  ↔ Analyst_History!C (timestamp)
        — parsed-date equality (drops time-of-day from AH side)

Collisions: same (agent, date) can legitimately appear in multiple
Scores rows (an analyst scoring two calls in one day). The script
matches FIFO — Analyst_History rows in document order consume Scores
rows for the same bucket in document order.

Garbage filter on Scores rows (skipped without failing the run):
    - blank Score / Agent / Date
    - non-numeric Score ("#REF!", text)
    - duplicate header rows (literal "Call Date" / "Agent" cells)

Dry-run by default. `--apply` commits the F-column update as a single
batch write (one API call for all 242 cells).

Usage:
    # Dry-run: prints match counts + unmatched samples, no writes
    python qa-automation/scripts/backfill_sales_overall_scores.py

    # Commit:
    python qa-automation/scripts/backfill_sales_overall_scores.py --apply

    # Show first 50 matches and all unmatched in detail:
    python qa-automation/scripts/backfill_sales_overall_scores.py -v
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import datetime

import migration_utils as mu

TEAM_ID = "sales"
SCORES_TAB = "Scores"

# 0-based column indices
SCORES_COL_LINK   = 0   # A
SCORES_COL_DATE   = 1   # B
SCORES_COL_AGENT  = 2   # C
SCORES_COL_SCORE  = 24  # Y

AH_COL_AGENT      = 0   # A
AH_COL_TIMESTAMP  = 2   # C
AH_COL_OVERALL    = 5   # F

# Date parse formats tried in order
_DATE_FORMATS = ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y", "%Y-%m-%dT%H:%M:%S")


def normalize_date(raw: str) -> str:
    """Parse a date-ish string and return MM/DD/YYYY canonical form.

    AH timestamps can include time-of-day ("5/10/2026 14:30:00"); Scores
    dates do not. Drop anything after the first space, then try each
    known format. On parse failure, return the stripped original so
    string equality can still rescue exact matches.
    """
    s = (raw or "").strip()
    if not s:
        return ""
    s = s.split(" ", 1)[0]
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).strftime("%m/%d/%Y")
        except ValueError:
            continue
    return s


def normalize_agent(raw: str) -> str:
    """Case-insensitive, whitespace-stripped form for the match key."""
    return (raw or "").strip().lower()


def is_numeric(s: str) -> bool:
    try:
        float(s)
        return True
    except (ValueError, TypeError):
        return False


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Back-fill overall_score on Sales Analyst_History from Scores!Y.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Commit the F-column update (default: dry run).",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Print first 50 matches and all unmatched rows in detail.",
    )
    args = parser.parse_args()

    cfg, layout = mu.history_layout_for(TEAM_ID)
    ah_tab = cfg.sheets.analyst_history.tab_name

    mu.print_banner(
        title="Sales overall_score backfill",
        fields={
            "Team":         TEAM_ID,
            "Source tab":   SCORES_TAB,
            "Source key":   "Scores!C (Agent) + Scores!B (Date)",
            "Source value": "Scores!Y (Score)",
            "Target tab":   ah_tab,
            "Target key":   "AH!A (agent_name) + AH!C (timestamp)",
            "Target col":   "AH!F (overall_score)",
            "Mode":         "APPLY (writes will execute)" if args.apply else "DRY RUN",
        },
        dry_run=not args.apply,
    )

    ss = mu.open_team_spreadsheet(TEAM_ID)
    scores_ws = mu.get_worksheet(ss, SCORES_TAB)
    if scores_ws is None:
        print(f"ERROR: '{SCORES_TAB}' tab not found on Sales spreadsheet.")
        return 2
    ah_ws = mu.get_worksheet(ss, ah_tab)
    if ah_ws is None:
        print(f"ERROR: '{ah_tab}' tab not found on Sales spreadsheet.")
        return 2

    # 1) Read both tabs
    print(f"  Reading '{SCORES_TAB}'...")
    scores_rows = scores_ws.get_all_values()
    print(f"    -> {len(scores_rows)} rows total")

    print(f"  Reading '{ah_tab}'...")
    ah_rows = ah_ws.get_all_values()
    print(f"    -> {len(ah_rows)} rows total ({len(ah_rows) - 1} data)")

    # 2) Build Scores lookup, filtering garbage
    print(f"  Indexing Scores rows...")
    scores_index: dict[tuple[str, str], list[tuple[str, str, int]]] = defaultdict(list)
    skipped_no_score = skipped_no_agent_date = skipped_dup_header = skipped_non_numeric = 0

    for i, r in enumerate(scores_rows[1:], start=2):
        date_raw  = r[SCORES_COL_DATE]  if len(r) > SCORES_COL_DATE  else ""
        agent_raw = r[SCORES_COL_AGENT] if len(r) > SCORES_COL_AGENT else ""
        score_raw = r[SCORES_COL_SCORE] if len(r) > SCORES_COL_SCORE else ""
        link      = r[SCORES_COL_LINK]  if len(r) > SCORES_COL_LINK  else ""

        # Skip the literal-header row that sometimes appears mid-sheet
        if date_raw.strip() == "Call Date" or agent_raw.strip() == "Agent":
            skipped_dup_header += 1
            continue
        if not score_raw.strip():
            skipped_no_score += 1
            continue
        if not is_numeric(score_raw):
            skipped_non_numeric += 1
            continue
        if not date_raw.strip() or not agent_raw.strip():
            skipped_no_agent_date += 1
            continue

        key = (normalize_agent(agent_raw), normalize_date(date_raw))
        scores_index[key].append((score_raw.strip(), link, i))

    indexed = sum(len(v) for v in scores_index.values())
    print(f"    -> indexed: {indexed} eligible Scores rows")
    print(f"    -> skipped: {skipped_no_score} blank-score, {skipped_non_numeric} non-numeric,")
    print(f"                {skipped_no_agent_date} missing-key, {skipped_dup_header} dup-header")

    # 3) Match AH rows against the Scores index, FIFO within (agent, date)
    print(f"  Matching AH rows against Scores...")
    # ah_new_f is a list of length len(ah_rows). Index 0 is header
    # (untouched). Indexes 1..N hold the new F values for each data row.
    ah_new_f: list[str] = [r[AH_COL_OVERALL] if len(r) > AH_COL_OVERALL else ""
                            for r in ah_rows]

    matched: list[tuple[int, str, str, str]] = []
    unmatched: list[tuple[int, str, str]] = []

    for i, r in enumerate(ah_rows[1:], start=2):
        agent_raw = r[AH_COL_AGENT]     if len(r) > AH_COL_AGENT     else ""
        ts_raw    = r[AH_COL_TIMESTAMP] if len(r) > AH_COL_TIMESTAMP else ""

        key = (normalize_agent(agent_raw), normalize_date(ts_raw))
        bucket = scores_index.get(key)
        if bucket:
            score, _, scores_row_num = bucket.pop(0)
            ah_new_f[i - 1] = score
            matched.append((i, agent_raw, ts_raw, score))
        else:
            unmatched.append((i, agent_raw, ts_raw))

    leftover = sum(len(v) for v in scores_index.values())

    print(f"    -> matched:   {len(matched)}/{len(ah_rows) - 1} AH rows")
    print(f"    -> unmatched: {len(unmatched)} AH rows (will stay blank)")
    print(f"    -> leftover:  {leftover} Scores entries no AH row claimed")

    if args.verbose:
        print()
        print(f"  First 50 matches:")
        for row_num, agent, ts, score in matched[:50]:
            print(f"    AH row {row_num:3d}: {agent!r:20s} @ {ts!r:25s} <- score {score!r}")
        if unmatched:
            print()
            print(f"  All {len(unmatched)} unmatched AH rows:")
            for row_num, agent, ts in unmatched:
                print(f"    AH row {row_num:3d}: {agent!r:20s} @ {ts!r}")

    elif unmatched:
        print()
        print(f"  First {min(10, len(unmatched))} unmatched AH rows (re-run with -v for full list):")
        for row_num, agent, ts in unmatched[:10]:
            print(f"    AH row {row_num:3d}: {agent!r:20s} @ {ts!r}")

    # 4) Dry-run stops here
    if not args.apply:
        print()
        print(f"  Dry run complete. Would write {len(matched)} new overall_score values to "
              f"'{ah_tab}' col F.")
        print(f"  Re-run with --apply to commit.")
        return 0

    # 5) Apply: single batch write of F2:F<N> as a column
    if not mu.confirm_apply():
        print("  Aborted by user.")
        return 1

    last_row = len(ah_rows)  # 1-indexed inclusive
    f_values = [[v] for v in ah_new_f[1:]]  # drop header
    range_a1 = f"F2:F{last_row}"
    print(f"  Writing {range_a1} ({len(f_values)} cells) in a single batch call...")
    ah_ws.update(range_a1, f_values, value_input_option="USER_ENTERED")
    print(f"    -> done.")
    print()
    print(f"  Backfill complete. {len(matched)} rows populated; {len(unmatched)} stay blank.")
    print(f"  Leftover Scores entries ({leftover}) suggest either test data,")
    print(f"  duplicate scores, or AH rows missing for those calls.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

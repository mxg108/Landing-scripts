"""Shared helpers for the Phase E migration scripts.

Pattern (used by both migrate_ms_history.py and import_sales_history.py):

    1. Load env + connect to the team's spreadsheet
    2. Read the source tab in one call (get_all_values)
    3. Transform rows in memory
    4. **Dry-run by default**: print what would be written, abort.
    5. **With --apply**: write to a *_PENDING tab, run verification
       gates, rename PENDING -> final tab name.

Single-write-call strategy stays under the Sheets 60-writes/min limit
even on the largest expected MS row count (~2000) by chunking
append_rows into 500-row batches with a small inter-chunk pause.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Ensure backend.* is importable when scripts/ runs from the repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_AI_SCORING = _REPO_ROOT / "qa-automation" / "AI-Scoring"
sys.path.insert(0, str(_AI_SCORING))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_AI_SCORING / ".env")

from gspread.exceptions import APIError, WorksheetNotFound  # noqa: E402

from backend.config.history_layout import (  # noqa: E402
    HistoryLayout,
    col_index_to_letter,
)
from backend.config.team_config import TeamConfig, get_team_config  # noqa: E402
from backend.services.sheets_service import _get_spreadsheet  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PENDING_SUFFIX = "_PENDING"
CHUNK_SIZE = 500
INTER_CHUNK_SLEEP_S = 1.2  # ~50 writes/min sustained; well under the 60/min cap

DIALPAD_CACHE_DIR = _REPO_ROOT / ".dialpad_cache"
DIALPAD_RATE_LIMIT_SLEEP_S = 0.2  # 5 req/sec when cold-cache


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------

def open_team_spreadsheet(team_id: str):
    """Open the team's spreadsheet using the same env + auth as the backend."""
    return _get_spreadsheet(team_id)


def get_worksheet(spreadsheet, tab_name: str):
    """Return the worksheet if it exists, else None (does not raise)."""
    try:
        return spreadsheet.worksheet(tab_name)
    except WorksheetNotFound:
        return None


def create_target_tab(spreadsheet, tab_name: str, n_rows: int, n_cols: int):
    """Create a fresh tab sized for the migration. Caller must have ensured
    no tab with this name already exists (use abort_if_tab_exists first)."""
    return spreadsheet.add_worksheet(title=tab_name, rows=max(n_rows, 1), cols=n_cols)


def abort_if_tab_exists(spreadsheet, tab_name: str, force: bool) -> None:
    """Bail out (or delete-and-recreate if --force) when the target tab
    already exists.  Avoids accidentally clobbering a previous run's data."""
    ws = get_worksheet(spreadsheet, tab_name)
    if ws is None:
        return
    if not force:
        print(
            f"ERROR: tab '{tab_name}' already exists in this spreadsheet.\n"
            f"  -> A previous run may have left it behind.\n"
            f"  -> Either delete the tab manually, or re-run with --force "
            f"to delete + recreate.",
            file=sys.stderr,
        )
        sys.exit(2)
    print(f"  --force set: deleting existing '{tab_name}' tab before recreating...")
    spreadsheet.del_worksheet(ws)


def rename_worksheet(worksheet, new_name: str) -> None:
    """Rename via gspread's update_title."""
    worksheet.update_title(new_name)


# ---------------------------------------------------------------------------
# Batch writer with backoff
# ---------------------------------------------------------------------------

def chunked_append_rows(
    worksheet,
    rows: list[list],
    chunk_size: int = CHUNK_SIZE,
    sleep_s: float = INTER_CHUNK_SLEEP_S,
    verbose: bool = False,
) -> int:
    """Append rows to *worksheet* in chunks of *chunk_size*.

    Each chunk is one `append_rows` API call. Pauses between chunks to stay
    under the Sheets 60-writes/min quota. On a 429 (RESOURCE_EXHAUSTED),
    sleeps and retries the same chunk up to 3 times with exponential backoff.

    Returns the total number of rows written (post-condition: should equal
    ``len(rows)`` unless an unrecoverable error occurred).
    """
    if not rows:
        return 0

    total = len(rows)
    written = 0
    chunks = [rows[i : i + chunk_size] for i in range(0, total, chunk_size)]

    for idx, chunk in enumerate(chunks, start=1):
        attempt = 0
        backoff = 2.0
        while True:
            try:
                worksheet.append_rows(chunk, value_input_option="USER_ENTERED")
                written += len(chunk)
                if verbose or len(chunks) > 1:
                    print(f"  chunk {idx}/{len(chunks)}: wrote {len(chunk)} rows "
                          f"({written}/{total} total)")
                break
            except APIError as e:
                # 429 = RESOURCE_EXHAUSTED; retry with backoff
                status = getattr(e.response, "status_code", None) if hasattr(e, "response") else None
                if status == 429 and attempt < 3:
                    attempt += 1
                    wait = backoff ** attempt
                    print(f"  chunk {idx}: 429 rate-limited; backing off {wait:.1f}s "
                          f"(attempt {attempt}/3)")
                    time.sleep(wait)
                    continue
                raise

        if idx < len(chunks):
            time.sleep(sleep_s)

    return written


# ---------------------------------------------------------------------------
# Verification helpers
# ---------------------------------------------------------------------------

@dataclass
class VerificationResult:
    """Outcome of a single verification gate."""
    name: str
    passed: bool
    detail: str = ""


@dataclass
class VerificationReport:
    """Aggregated outcome of all gates for a single migration run."""
    results: list[VerificationResult] = field(default_factory=list)

    def add(self, name: str, passed: bool, detail: str = "") -> None:
        self.results.append(VerificationResult(name=name, passed=passed, detail=detail))

    @property
    def all_passed(self) -> bool:
        return all(r.passed for r in self.results)

    def print_summary(self) -> None:
        print()
        print("  Verification gates:")
        for r in self.results:
            symbol = "PASS" if r.passed else "FAIL"
            print(f"    [{symbol}] {r.name}" + (f" — {r.detail}" if r.detail else ""))
        print()
        print(f"  -> {'all gates passed' if self.all_passed else 'one or more gates failed'}")


# ---------------------------------------------------------------------------
# Banner / summary printing
# ---------------------------------------------------------------------------

def print_banner(title: str, fields: dict[str, str], dry_run: bool) -> None:
    """Pre-flight summary that mirrors the push.sh confirmation prompt."""
    print()
    print("=" * 60)
    print(f"  {title}")
    if dry_run:
        print("  -- DRY RUN (no writes will execute; pass --apply to commit) --")
    print("=" * 60)
    print()
    for k, v in fields.items():
        print(f"  {k:18s}: {v}")
    print()


def confirm_apply() -> bool:
    """Final yes/no gate before actually writing in --apply mode."""
    try:
        ans = input("  Type 'yes' to proceed with writes: ").strip()
    except (EOFError, KeyboardInterrupt):
        return False
    return ans == "yes"


# ---------------------------------------------------------------------------
# Dialpad backfill (Sales only)
# ---------------------------------------------------------------------------

_CALL_ID_RE = None


def _extract_call_id(dialpad_link: str) -> Optional[str]:
    """Pull the call_id out of a Dialpad URL.

    Supports URL patterns like:
      https://dialpad.com/app/call/<call_id>
      https://*.dialpad.com/call/<call_id>
      https://dialpad.com/call/<call_id>?foo=bar

    Returns None if no plausible call_id found.
    """
    import re
    global _CALL_ID_RE
    if _CALL_ID_RE is None:
        _CALL_ID_RE = re.compile(r"/call/(\d+)(?:[?/]|$)")
    if not dialpad_link:
        return None
    m = _CALL_ID_RE.search(dialpad_link)
    return m.group(1) if m else None


async def _fetch_call_details_cached(call_id: str, cache_dir: Path) -> dict:
    """Fetch (and cache to disk) the Dialpad call-details payload."""
    cache_file = cache_dir / f"{call_id}.json"
    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text())
        except Exception:
            # Corrupt cache entry — re-fetch
            pass

    from backend.services.dialpad_client import get_call_details
    data = await get_call_details(call_id)
    cache_file.write_text(json.dumps(data))
    # Self-throttle when actually hitting the API
    await asyncio.sleep(DIALPAD_RATE_LIMIT_SLEEP_S)
    return data


async def backfill_dates_async(
    needed: list[tuple[str, str]],
    cache_dir: Path,
) -> dict[str, str]:
    """Look up date_connected for each (row_id, dialpad_link).

    Sequential (not concurrent) — the rate budget is small and sequencing
    keeps the self-throttle deterministic. Cached calls return instantly.

    Returns ``{row_id: date_connected_iso}``; missing entries default to "".
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    out: dict[str, str] = {}
    for row_id, link in needed:
        call_id = _extract_call_id(link)
        if not call_id:
            out[row_id] = ""
            continue
        data = await _fetch_call_details_cached(call_id, cache_dir)
        out[row_id] = data.get("date_connected", "") or ""
    return out


def backfill_dates(
    needed: list[tuple[str, str]],
    cache_dir: Path = DIALPAD_CACHE_DIR,
) -> dict[str, str]:
    """Sync wrapper around the async backfill."""
    return asyncio.run(backfill_dates_async(needed, cache_dir))


# ---------------------------------------------------------------------------
# History-layout convenience
# ---------------------------------------------------------------------------

def history_layout_for(team_id: str) -> tuple[TeamConfig, HistoryLayout]:
    """Load team config + return the matching HistoryLayout."""
    cfg = get_team_config(team_id)
    return cfg, cfg.history_layout


def blank_history_row(layout: HistoryLayout) -> list[str]:
    """Make a fresh, fully-blank row sized to the team's layout."""
    return [""] * layout.total_width


def column_letter_range(layout: HistoryLayout) -> str:
    """A1-style end column letter (e.g. 'AP' for MS, 'BQ' for Sales)."""
    return col_index_to_letter(layout.total_width - 1)

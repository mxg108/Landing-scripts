"""Shared data-normalization helpers for analytics + future predictive ETL.

Pulled out of team_stats.py so the same rules apply when (a) the live
dashboard reads from Sheets today and (b) the predictive pipeline reads
from Postgres after Step 9 / Local AI cutover. Keeping accent stripping,
timestamp parsing, and test-row filtering in one place is the cheapest
way to avoid drift between the two paths.
"""

from __future__ import annotations

import unicodedata
from datetime import datetime
from typing import Iterable, Optional


# Timestamp formats encountered in Analyst_History. Order matters: the
# parser tries each in turn and returns the first match.
_TS_FORMATS = (
    "%m/%d/%Y %H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%m/%d/%Y %H:%M",
    "%Y-%m-%dT%H:%M:%S",
)


def strip_accents(s: str) -> str:
    """Remove combining marks for case-insensitive comparison.

    'León' → 'Leon'.  Used for agent-name canonicalization where
    typists, form auto-fill, and Dialpad sometimes disagree.
    """
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def parse_timestamp(val: object) -> Optional[datetime]:
    """Parse a Sheets timestamp string into a datetime, or None if unparseable."""
    text = str(val).strip()
    for fmt in _TS_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except (ValueError, AttributeError):
            continue
    return None


def is_excluded_test_row(
    agent_name: str,
    overall_score: float,
    excluded_agents: Iterable[str],
) -> bool:
    """Return True if this row is a test entry that should be filtered.

    Convention: a row is filtered iff the (accent-stripped, lower-cased)
    agent name matches an entry in ``excluded_agents`` AND the overall
    score is exactly 0.  The score-0 sentinel preserves the original
    behavior — a real evaluation of a person whose name happens to
    appear in the exclusion list still flows through analytics.

    When 100% Local AI scoring lands the score-0 sentinel may be
    revisited; for now we keep semantics identical to the legacy filter.
    """
    if overall_score != 0:
        return False
    needle = strip_accents(agent_name).lower()
    for excluded in excluded_agents:
        if needle.startswith(strip_accents(excluded).lower()):
            return True
    return False

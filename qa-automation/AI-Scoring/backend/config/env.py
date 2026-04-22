"""Team-scoped environment variable lookup.

Convention: per-team overrides take the form ``{BASE}_{TEAM_ID_UPPER}``
(e.g. ``GOOGLE_SHEETS_ID_SALES``).  For ``member_support``, callers can
opt into a legacy fallback to the flat ``{BASE}`` name so the existing
single-tenant ``.env`` continues to work unchanged.
"""

from __future__ import annotations

import os


def env_for_team(base: str, team_id: str, legacy_ok: bool = False, default: str = "") -> str:
    """Return the env var value for *base* scoped to *team_id*.

    Lookup order:
      1. ``{base}_{team_id.upper()}``
      2. If ``legacy_ok`` and team_id == 'member_support': bare ``{base}``
      3. ``default``
    """
    val = os.getenv(f"{base}_{team_id.upper()}", "")
    if val:
        return val
    if legacy_ok and team_id == "member_support":
        return os.getenv(base, default)
    return default

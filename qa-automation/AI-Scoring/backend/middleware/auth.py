"""API key authentication middleware.

Two key tiers:

* **Team keys** — env vars named ``API_KEY_{TEAM_ID}`` (e.g.
  ``API_KEY_MEMBER_SUPPORT``, ``API_KEY_SALES``). The suffix becomes the
  ``team_id`` the key is bound to; routes under ``/api/{team_id}/...``
  enforce that the path matches.
* **Privileged key** — env var ``API_KEY_PRIVILEGED``. Not bound to any
  team. Used by leadership / HR / dev tooling that needs to score or
  inspect calls across teams. Bypasses team-scoping on every route that
  applies it.

Adding more privileged tiers later (e.g. ``API_KEY_HR``) is a one-line
change in ``_PRIVILEGED_SUFFIXES``.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from typing import Literal, Optional

from fastapi import Depends, Header, HTTPException, Request


_PRIVILEGED_SUFFIXES = {"privileged"}


@dataclass(frozen=True)
class KeyIdentity:
    """Resolved identity attached to a validated API key."""
    role: Literal["team", "privileged"]
    team_id: Optional[str]  # None for privileged keys


def _build_key_map() -> dict[str, KeyIdentity]:
    """Build mapping of API key -> KeyIdentity from environment variables.

    Looks for env vars named ``API_KEY_{SUFFIX}``. Suffix ``PRIVILEGED``
    yields ``role="privileged"`` with no team binding; any other suffix
    is treated as a team_id (lowercased).
    """
    mapping: dict[str, KeyIdentity] = {}
    prefix = "API_KEY_"
    for key, value in os.environ.items():
        if not key.startswith(prefix) or not value:
            continue
        suffix = key[len(prefix):].lower()
        if suffix in _PRIVILEGED_SUFFIXES:
            mapping[value] = KeyIdentity(role="privileged", team_id=None)
        else:
            mapping[value] = KeyIdentity(role="team", team_id=suffix)
    return mapping


# Built once at import time; reload requires restart
_KEY_MAP = _build_key_map()

if not _KEY_MAP:
    import logging
    logging.warning(
        "No API keys configured (expected API_KEY_MEMBER_SUPPORT in env). "
        "All authenticated requests will return 401."
    )


async def require_api_key(authorization: str = Header(None)) -> KeyIdentity:
    """FastAPI dependency that validates the Bearer token and returns its identity."""
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")

    token = authorization.removeprefix("Bearer ").strip()
    if token == authorization:
        raise HTTPException(status_code=401, detail="Authorization header must use Bearer scheme")

    for known_key, identity in _KEY_MAP.items():
        if secrets.compare_digest(token, known_key):
            return identity

    raise HTTPException(status_code=401, detail="Invalid API key")


async def require_team_access(team_id: str, authorization: str = Header(None)) -> KeyIdentity:
    """Validate the key and enforce team scoping unless the key is privileged.

    Privileged keys bypass the team check on every team-prefixed route.
    Returns the resolved KeyIdentity so downstream handlers can branch on
    role (e.g. /score uses it to decide whether to enforce roster membership).
    """
    identity = await require_api_key(authorization)
    if identity.role == "privileged":
        return identity
    if identity.team_id != team_id:
        raise HTTPException(
            status_code=403,
            detail=f"API key not authorized for team '{team_id}'",
        )
    return identity


def check_scoring_access(
    identity: KeyIdentity,
    team_id: str,
    agent_email: Optional[str],
    *,
    is_in_roster: bool,
) -> None:
    """Raise 403 unless ``identity`` is allowed to score ``agent_email`` for ``team_id``.

    Called from ``/score`` after the multipart form has been parsed, so
    it can't be a ``Depends``-style FastAPI dependency. The roster
    membership is supplied as a precomputed bool — the handler does the
    async Mails fetch and passes the result here. This keeps auth.py
    free of service imports while avoiding awkward sync/async bridging.

    Rules:
      * Team key: must match ``team_id`` AND ``is_in_roster`` must be True.
      * Privileged key: any real ``team_id`` is fine; roster membership
        is NOT required (the frontend confirms intent via the team-pick
        dialog when the agent is unrostered).
    """
    if identity.role == "privileged":
        return
    if identity.team_id != team_id:
        raise HTTPException(
            status_code=403,
            detail=f"API key not authorized for team '{team_id}'",
        )
    if not agent_email or not is_in_roster:
        raise HTTPException(
            status_code=403,
            detail=f"Agent '{agent_email}' is not in team '{team_id}' roster",
        )


AUTH_DEPENDENCY = [Depends(require_api_key)]
TEAM_AUTH_DEPENDENCY = [Depends(require_team_access)]


def team_id_from_path(request: Request) -> str:
    """Return team_id from the URL path.

    For team-prefixed routes this resolves to the path param.  For legacy
    routes (no ``{team_id}`` in the path) it defaults to ``member_support``
    so the single-tenant form keeps working during the 30-day transition.
    """
    return request.path_params.get("team_id", "member_support")

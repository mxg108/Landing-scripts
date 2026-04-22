"""API key authentication middleware."""

import os
import secrets

from fastapi import Depends, Header, HTTPException, Request


def _build_key_map() -> dict[str, str]:
    """Build mapping of API key -> team_id from environment variables.

    Looks for env vars named API_KEY_{TEAM_ID} (e.g. API_KEY_MEMBER_SUPPORT).
    Forward-compatible with multi-team: add more env vars, get more mappings.
    """
    mapping = {}
    prefix = "API_KEY_"
    for key, value in os.environ.items():
        if key.startswith(prefix) and value:
            team_id = key[len(prefix):].lower()
            mapping[value] = team_id
    return mapping


# Built once at import time; reload requires restart
_KEY_MAP = _build_key_map()

if not _KEY_MAP:
    import logging
    logging.warning(
        "No API keys configured (expected API_KEY_MEMBER_SUPPORT in env). "
        "All authenticated requests will return 401."
    )


async def require_api_key(authorization: str = Header(None)) -> str:
    """FastAPI dependency that validates the Bearer token and returns team_id."""
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")

    token = authorization.removeprefix("Bearer ").strip()
    if token == authorization:
        # No "Bearer " prefix found
        raise HTTPException(status_code=401, detail="Authorization header must use Bearer scheme")

    # Timing-safe comparison against all known keys
    for known_key, team_id in _KEY_MAP.items():
        if secrets.compare_digest(token, known_key):
            return team_id

    raise HTTPException(status_code=401, detail="Invalid API key")


async def require_team_access(team_id: str, authorization: str = Header(None)) -> str:
    """Validate the key *and* enforce it matches the team_id in the URL.

    Binds ``team_id`` automatically from the path param of the route it
    protects, so a Sales key cannot reach Member Support data (and vice
    versa).  Returns the resolved team_id.
    """
    key_team = await require_api_key(authorization)
    if key_team != team_id:
        raise HTTPException(
            status_code=403,
            detail=f"API key not authorized for team '{team_id}'",
        )
    return team_id


AUTH_DEPENDENCY = [Depends(require_api_key)]
TEAM_AUTH_DEPENDENCY = [Depends(require_team_access)]


def team_id_from_path(request: Request) -> str:
    """Return team_id from the URL path.

    For team-prefixed routes this resolves to the path param.  For legacy
    routes (no ``{team_id}`` in the path) it defaults to ``member_support``
    so the single-tenant form keeps working during the 30-day transition.
    """
    return request.path_params.get("team_id", "member_support")

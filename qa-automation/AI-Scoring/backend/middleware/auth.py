"""API key authentication middleware."""

import os
import secrets

from fastapi import Depends, Header, HTTPException


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


AUTH_DEPENDENCY = [Depends(require_api_key)]

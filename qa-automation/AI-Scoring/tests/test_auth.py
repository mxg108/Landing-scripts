"""Auth middleware unit tests — API key tiers, team scoping, scoring access.

``_build_key_map`` reads ``os.environ`` at import time, so tests that want
a specific key map call it explicitly via ``monkeypatch.setenv`` + a fresh
invocation rather than relying on the module-level ``_KEY_MAP``.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from backend.middleware import auth
from backend.middleware.auth import KeyIdentity, check_scoring_access


# ---------------------------------------------------------------------------
# _build_key_map
# ---------------------------------------------------------------------------

def test_build_key_map_team_suffix_yields_team_identity(monkeypatch):
    for k in list(__import__("os").environ):
        if k.startswith("API_KEY_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("API_KEY_MEMBER_SUPPORT", "ms-secret")
    monkeypatch.setenv("API_KEY_SALES", "sales-secret")
    mapping = auth._build_key_map()
    assert mapping["ms-secret"] == KeyIdentity(role="team", team_id="member_support")
    assert mapping["sales-secret"] == KeyIdentity(role="team", team_id="sales")


def test_build_key_map_privileged_suffix_yields_privileged_identity(monkeypatch):
    for k in list(__import__("os").environ):
        if k.startswith("API_KEY_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("API_KEY_PRIVILEGED", "priv-secret")
    mapping = auth._build_key_map()
    assert mapping["priv-secret"] == KeyIdentity(role="privileged", team_id=None)


def test_build_key_map_skips_empty_values(monkeypatch):
    monkeypatch.setenv("API_KEY_MEMBER_SUPPORT", "")
    mapping = auth._build_key_map()
    assert "" not in mapping


# ---------------------------------------------------------------------------
# require_api_key + require_team_access
# ---------------------------------------------------------------------------

def test_require_api_key_returns_key_identity(monkeypatch):
    monkeypatch.setattr(auth, "_KEY_MAP", {
        "team-tok": KeyIdentity(role="team", team_id="member_support"),
    })
    identity = asyncio.run(auth.require_api_key(authorization="Bearer team-tok"))
    assert identity == KeyIdentity(role="team", team_id="member_support")


def test_require_api_key_rejects_missing_header():
    with pytest.raises(HTTPException) as exc:
        asyncio.run(auth.require_api_key(authorization=None))
    assert exc.value.status_code == 401


def test_require_api_key_rejects_missing_bearer_prefix(monkeypatch):
    monkeypatch.setattr(auth, "_KEY_MAP", {
        "tok": KeyIdentity(role="team", team_id="member_support"),
    })
    with pytest.raises(HTTPException) as exc:
        asyncio.run(auth.require_api_key(authorization="tok"))
    assert exc.value.status_code == 401


def test_require_api_key_rejects_unknown_token(monkeypatch):
    monkeypatch.setattr(auth, "_KEY_MAP", {
        "tok": KeyIdentity(role="team", team_id="member_support"),
    })
    with pytest.raises(HTTPException) as exc:
        asyncio.run(auth.require_api_key(authorization="Bearer wrong"))
    assert exc.value.status_code == 401


# ---------------------------------------------------------------------------
# ?api_key= query-param fallback (Phase 0 — for SSE EventSource which
# cannot set Authorization headers).
# ---------------------------------------------------------------------------

def test_require_api_key_accepts_query_param(monkeypatch):
    monkeypatch.setattr(auth, "_KEY_MAP", {
        "team-tok": KeyIdentity(role="team", team_id="member_support"),
    })
    identity = asyncio.run(auth.require_api_key(authorization=None, api_key="team-tok"))
    assert identity == KeyIdentity(role="team", team_id="member_support")


def test_require_api_key_query_param_rejects_unknown_token(monkeypatch):
    monkeypatch.setattr(auth, "_KEY_MAP", {
        "tok": KeyIdentity(role="team", team_id="member_support"),
    })
    with pytest.raises(HTTPException) as exc:
        asyncio.run(auth.require_api_key(authorization=None, api_key="wrong"))
    assert exc.value.status_code == 401


def test_require_api_key_header_preferred_over_query_when_both_present(monkeypatch):
    """Header wins so a client that already authenticates via Authorization
    can't be downgraded by a tampered URL. The query-param path is purely a
    fallback for EventSource."""
    monkeypatch.setattr(auth, "_KEY_MAP", {
        "header-tok": KeyIdentity(role="team", team_id="sales"),
        "query-tok": KeyIdentity(role="team", team_id="member_support"),
    })
    identity = asyncio.run(auth.require_api_key(
        authorization="Bearer header-tok", api_key="query-tok",
    ))
    assert identity.team_id == "sales"


def test_require_api_key_rejects_when_neither_provided():
    with pytest.raises(HTTPException) as exc:
        asyncio.run(auth.require_api_key(authorization=None, api_key=None))
    assert exc.value.status_code == 401


def test_require_team_access_accepts_query_param(monkeypatch):
    monkeypatch.setattr(auth, "_KEY_MAP", {
        "ms-tok": KeyIdentity(role="team", team_id="member_support"),
    })
    identity = asyncio.run(auth.require_team_access(
        team_id="member_support", authorization=None, api_key="ms-tok",
    ))
    assert identity.team_id == "member_support"


def test_require_team_access_team_key_matching_team_passes(monkeypatch):
    monkeypatch.setattr(auth, "_KEY_MAP", {
        "ms-tok": KeyIdentity(role="team", team_id="member_support"),
    })
    identity = asyncio.run(auth.require_team_access(
        team_id="member_support", authorization="Bearer ms-tok"
    ))
    assert identity.role == "team"


def test_require_team_access_team_key_cross_team_rejected(monkeypatch):
    monkeypatch.setattr(auth, "_KEY_MAP", {
        "ms-tok": KeyIdentity(role="team", team_id="member_support"),
    })
    with pytest.raises(HTTPException) as exc:
        asyncio.run(auth.require_team_access(
            team_id="sales", authorization="Bearer ms-tok"
        ))
    assert exc.value.status_code == 403


def test_require_team_access_privileged_bypasses(monkeypatch):
    monkeypatch.setattr(auth, "_KEY_MAP", {
        "priv-tok": KeyIdentity(role="privileged", team_id=None),
    })
    identity = asyncio.run(auth.require_team_access(
        team_id="sales", authorization="Bearer priv-tok"
    ))
    assert identity.role == "privileged"


# ---------------------------------------------------------------------------
# check_scoring_access
# ---------------------------------------------------------------------------

def test_check_scoring_access_team_key_in_roster_allows():
    key = KeyIdentity(role="team", team_id="member_support")
    check_scoring_access(
        key, "member_support", "luis@landing.com", is_in_roster=True,
    )


def test_check_scoring_access_team_key_unrostered_rejected():
    key = KeyIdentity(role="team", team_id="member_support")
    with pytest.raises(HTTPException) as exc:
        check_scoring_access(
            key, "member_support", "unknown@landing.com", is_in_roster=False,
        )
    assert exc.value.status_code == 403


def test_check_scoring_access_team_key_cross_team_rejected():
    key = KeyIdentity(role="team", team_id="member_support")
    with pytest.raises(HTTPException) as exc:
        check_scoring_access(
            key, "sales", "luis@landing.com", is_in_roster=True,
        )
    assert exc.value.status_code == 403


def test_check_scoring_access_privileged_bypasses_roster():
    key = KeyIdentity(role="privileged", team_id=None)
    check_scoring_access(
        key, "sales", "contractor@external.com", is_in_roster=False,
    )


def test_check_scoring_access_team_key_missing_email_rejected():
    key = KeyIdentity(role="team", team_id="member_support")
    with pytest.raises(HTTPException) as exc:
        check_scoring_access(
            key, "member_support", None, is_in_roster=True,
        )
    assert exc.value.status_code == 403

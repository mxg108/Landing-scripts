"""/lookup/scoring-permission route tests — PR 4 (LookupToScore.md)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.middleware import auth
from backend.middleware.auth import KeyIdentity
from backend.routes import lookup as lookup_module


TEAM_MS_TOKEN = "team-ms"
TEAM_SALES_TOKEN = "team-sales"
PRIV_TOKEN = "priv-tok"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(auth, "_KEY_MAP", {
        TEAM_MS_TOKEN: KeyIdentity(role="team", team_id="member_support"),
        TEAM_SALES_TOKEN: KeyIdentity(role="team", team_id="sales"),
        PRIV_TOKEN: KeyIdentity(role="privileged", team_id=None),
    })

    # Stub Mails lookup: luis @ MS, ada @ sales, contractor unrostered.
    async def fake_resolve(email):
        if email == "luis@landing.com":
            return "member_support"
        if email == "ada@landing.com":
            return "sales"
        return None

    monkeypatch.setattr(lookup_module, "resolve_team_for_agent", fake_resolve)

    from fastapi import FastAPI
    from backend.middleware.auth import AUTH_DEPENDENCY, TEAM_AUTH_DEPENDENCY

    app = FastAPI()
    app.include_router(
        lookup_module.router,
        prefix="/api/{team_id}",
        dependencies=TEAM_AUTH_DEPENDENCY,
    )
    app.include_router(
        lookup_module.router,
        prefix="/api",
        dependencies=AUTH_DEPENDENCY,
    )
    return TestClient(app)


# ---------------------------------------------------------------------------
# Team key
# ---------------------------------------------------------------------------

def test_team_key_in_roster_can_score(client):
    resp = client.get(
        "/api/member_support/lookup/scoring-permission",
        params={"email": "luis@landing.com"},
        headers={"Authorization": f"Bearer {TEAM_MS_TOKEN}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {
        "agent_email": "luis@landing.com",
        "resolved_team": "member_support",
        "can_score": True,
        "needs_team_pick": False,
    }


def test_team_key_cross_team_cannot_score(client):
    """MS key probing a Sales agent → can_score=False (path is /sales would
    fail auth; here we probe via /api/member_support path so the resolved
    team differs from the key's team)."""
    resp = client.get(
        "/api/member_support/lookup/scoring-permission",
        params={"email": "ada@landing.com"},
        headers={"Authorization": f"Bearer {TEAM_MS_TOKEN}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["resolved_team"] == "sales"
    assert body["can_score"] is False
    assert body["needs_team_pick"] is False


def test_team_key_unrostered_cannot_score(client):
    resp = client.get(
        "/api/member_support/lookup/scoring-permission",
        params={"email": "ghost@landing.com"},
        headers={"Authorization": f"Bearer {TEAM_MS_TOKEN}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["resolved_team"] is None
    assert body["can_score"] is False
    assert body["needs_team_pick"] is False


def test_team_key_path_mismatch_cannot_score(client):
    """A team key on the WRONG /api/{team_id} path is allowed through
    TEAM_AUTH_DEPENDENCY ONLY if it matches — so this case is actually
    a 403 from the middleware, not a can_score=False response."""
    resp = client.get(
        "/api/sales/lookup/scoring-permission",
        params={"email": "luis@landing.com"},
        headers={"Authorization": f"Bearer {TEAM_MS_TOKEN}"},
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Privileged key
# ---------------------------------------------------------------------------

def test_privileged_rostered_can_score_no_pick(client):
    resp = client.get(
        "/api/member_support/lookup/scoring-permission",
        params={"email": "luis@landing.com"},
        headers={"Authorization": f"Bearer {PRIV_TOKEN}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "agent_email": "luis@landing.com",
        "resolved_team": "member_support",
        "can_score": True,
        "needs_team_pick": False,
    }


def test_privileged_unrostered_needs_team_pick(client):
    resp = client.get(
        "/api/member_support/lookup/scoring-permission",
        params={"email": "contractor@external.com"},
        headers={"Authorization": f"Bearer {PRIV_TOKEN}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "agent_email": "contractor@external.com",
        "resolved_team": None,
        "can_score": True,
        "needs_team_pick": True,
    }


def test_privileged_crosses_teams_freely(client):
    """Privileged key on the /sales path probing an MS agent — still works."""
    resp = client.get(
        "/api/sales/lookup/scoring-permission",
        params={"email": "luis@landing.com"},
        headers={"Authorization": f"Bearer {PRIV_TOKEN}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["resolved_team"] == "member_support"
    assert body["can_score"] is True
    assert body["needs_team_pick"] is False  # rostered somewhere


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def test_unauth_returns_401(client):
    resp = client.get(
        "/api/member_support/lookup/scoring-permission",
        params={"email": "luis@landing.com"},
    )
    assert resp.status_code == 401


def test_missing_email_returns_422(client):
    """FastAPI returns 422 for missing required query param."""
    resp = client.get(
        "/api/member_support/lookup/scoring-permission",
        headers={"Authorization": f"Bearer {TEAM_MS_TOKEN}"},
    )
    assert resp.status_code == 422

"""/score route tests — extended behavior from PR 3 (LookupToScore.md).

Uses FastAPI's TestClient with monkeypatching of the imports inside
``backend.routes.scoring`` so no real I/O (Dialpad, Sheets, Gemini)
runs. The route's branching is what we're after — auth, idempotency,
audio fallback, audit-row write, 422/503 mapping.

The background task ``run()`` itself is never awaited in these tests;
they assert the route's *synchronous* observable behavior (response,
job-store entry, audit row appended).
"""

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
from backend.routes import scoring as scoring_module


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

TEAM_TOKEN = "team-ms-tok"
PRIV_TOKEN = "priv-tok"


@pytest.fixture
def team_key_identity() -> KeyIdentity:
    return KeyIdentity(role="team", team_id="member_support")


@pytest.fixture
def priv_key_identity() -> KeyIdentity:
    return KeyIdentity(role="privileged", team_id=None)


@pytest.fixture
def client(monkeypatch, team_key_identity, priv_key_identity):
    """FastAPI TestClient with auth and side-effects stubbed.

    Each test gets a fresh ``_jobs`` dict and an empty
    ``append_score_audit_row`` capture list at ``client.audit_rows``.
    """
    # Auth: register both tokens. require_team_access / require_api_key
    # both consult _KEY_MAP via secrets.compare_digest, so dict identity
    # doesn't matter — just the contents.
    monkeypatch.setattr(auth, "_KEY_MAP", {
        TEAM_TOKEN: team_key_identity,
        PRIV_TOKEN: priv_key_identity,
    })

    # Reset module-level job + semaphore state so tests don't leak.
    scoring_module._jobs.clear()
    scoring_module._key_semaphores.clear()

    # Capture audit appends.
    captured: list[dict] = []

    def fake_append(**kwargs):
        captured.append(kwargs)
        return 1

    monkeypatch.setattr(scoring_module, "append_score_audit_row", fake_append)

    # Stub Mails-roster check — declare luis@ in member_support.
    async def fake_email_in_team_mails(email, team_id):
        if not email:
            return False
        return (
            email.lower() == "luis@landing.com"
            and team_id == "member_support"
        )

    async def fake_agent_name_for_email(email, team_id):
        if email and email.lower() == "luis@landing.com":
            return "Luis Rubio"
        return None

    monkeypatch.setattr(scoring_module, "email_in_team_mails", fake_email_in_team_mails)
    monkeypatch.setattr(scoring_module, "agent_name_for_email", fake_agent_name_for_email)

    # Stub Dialpad metadata helpers so the success path doesn't hit the API.
    async def fake_get_transcript(call_id):
        return {"transcript": "(stub)"}

    async def fake_get_call_details(call_id):
        return {"call_id": call_id, "_flagged_long_call": False}

    monkeypatch.setattr(scoring_module, "get_transcript", fake_get_transcript)
    monkeypatch.setattr(scoring_module, "get_call_details", fake_get_call_details)

    # Stub the FastAPI app build so we don't need .env. Build a minimal
    # app that just mounts the scoring router exactly the way main.py does.
    from fastapi import FastAPI
    from backend.middleware.auth import AUTH_DEPENDENCY, TEAM_AUTH_DEPENDENCY

    app = FastAPI()
    app.include_router(
        scoring_module.router,
        prefix="/api/{team_id}",
        dependencies=TEAM_AUTH_DEPENDENCY,
    )
    app.include_router(
        scoring_module.router,
        prefix="/api",
        dependencies=AUTH_DEPENDENCY,
    )

    tc = TestClient(app)
    tc.audit_rows = captured  # attach for assertions
    return tc


# ---------------------------------------------------------------------------
# Audio-fallback path
# ---------------------------------------------------------------------------

def test_score_endpoint_without_audio_fetches_recording(client, monkeypatch):
    """Omitting audio_file triggers download_recording(call_id)."""
    called = {}

    async def fake_download(call_id):
        called["call_id"] = call_id
        return b"FAKERECORDINGBYTES"

    monkeypatch.setattr(scoring_module, "download_recording", fake_download)

    resp = client.post(
        "/api/member_support/score",
        headers={"Authorization": f"Bearer {TEAM_TOKEN}"},
        data={
            "call_id": "call-abc",
            "agent_email": "luis@landing.com",
            "manager_email": "ana@landing.com",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "pending"
    assert called == {"call_id": "call-abc"}


def test_score_endpoint_with_audio_skips_download(client, monkeypatch):
    """Supplying audio_file means download_recording must NOT be called."""
    called = {"hit": False}

    async def fake_download(call_id):
        called["hit"] = True
        return b""

    monkeypatch.setattr(scoring_module, "download_recording", fake_download)

    resp = client.post(
        "/api/member_support/score",
        headers={"Authorization": f"Bearer {TEAM_TOKEN}"},
        data={
            "call_id": "call-abc",
            "agent_email": "luis@landing.com",
            "manager_email": "ana@landing.com",
        },
        files={"audio_file": ("call.mp3", b"REALBYTES", "audio/mpeg")},
    )
    assert resp.status_code == 200, resp.text
    assert called["hit"] is False


def test_score_endpoint_no_recording_returns_422(client, monkeypatch):
    async def fake_download(call_id):
        from backend.services.dialpad_client import NoRecordingAvailable
        raise NoRecordingAvailable("none")

    monkeypatch.setattr(scoring_module, "download_recording", fake_download)

    resp = client.post(
        "/api/member_support/score",
        headers={"Authorization": f"Bearer {TEAM_TOKEN}"},
        data={
            "call_id": "call-aged-out",
            "agent_email": "luis@landing.com",
            "manager_email": "ana@landing.com",
        },
    )
    assert resp.status_code == 422
    assert "no recording" in resp.json()["detail"].lower()
    # Denial audit row should mention the reason.
    assert any(r["notes"] == "no_recording" for r in client.audit_rows)


def test_score_endpoint_rate_limited_returns_503(client, monkeypatch):
    async def fake_download(call_id):
        from backend.services.dialpad_client import DialpadRateLimited
        raise DialpadRateLimited("429")

    monkeypatch.setattr(scoring_module, "download_recording", fake_download)

    resp = client.post(
        "/api/member_support/score",
        headers={"Authorization": f"Bearer {TEAM_TOKEN}"},
        data={
            "call_id": "call-throttled",
            "agent_email": "luis@landing.com",
            "manager_email": "ana@landing.com",
        },
    )
    assert resp.status_code == 503
    assert any(r["notes"] == "rate_limited" for r in client.audit_rows)


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

def test_score_endpoint_idempotent_on_in_flight_job(client):
    """A POST while a prior job is 'scoring' returns the existing job_id and
    does NOT write a second audit row.

    Pre-seeds the job store so the test doesn't race the background task
    (which TestClient awaits before returning the response).
    """
    # job_id = f"{call_id}_{agent_name}".replace(" ", "_")
    # agent_name resolves from email "luis@landing.com" → "Luis Rubio"
    expected_job_id = "call-double-click_Luis_Rubio"
    scoring_module._jobs[
        scoring_module._job_key("member_support", expected_job_id)
    ] = {"status": "scoring", "call_id": "call-double-click"}

    resp = client.post(
        "/api/member_support/score",
        headers={"Authorization": f"Bearer {TEAM_TOKEN}"},
        data={
            "call_id": "call-double-click",
            "agent_email": "luis@landing.com",
            "manager_email": "ana@landing.com",
        },
        files={"audio_file": ("c.mp3", b"BYTES", "audio/mpeg")},
    )
    assert resp.status_code == 200
    assert resp.json() == {"job_id": expected_job_id, "status": "scoring"}
    # No audit row written — neither scored nor denied.
    assert client.audit_rows == []


# ---------------------------------------------------------------------------
# Audit row write
# ---------------------------------------------------------------------------

def test_score_endpoint_writes_audit_row_on_success(client, monkeypatch):
    async def fake_download(call_id):
        return b"BYTES"

    monkeypatch.setattr(scoring_module, "download_recording", fake_download)

    resp = client.post(
        "/api/member_support/score",
        headers={"Authorization": f"Bearer {TEAM_TOKEN}"},
        data={
            "call_id": "call-xyz",
            "agent_email": "luis@landing.com",
            "manager_email": "ana@landing.com",
        },
    )
    assert resp.status_code == 200
    assert len(client.audit_rows) == 1
    row = client.audit_rows[0]
    assert row["action"] == "scored"
    assert row["api_key_role"] == "team"
    assert row["evaluator_email"] == "ana@landing.com"
    assert row["agent_email"] == "luis@landing.com"
    assert row["agent_name"] == "Luis Rubio"  # resolved from email
    assert row["call_id"] == "call-xyz"
    assert row["target_team"] == "member_support"


def test_score_endpoint_writes_denied_audit_on_unrostered_team_key(client, monkeypatch):
    """Team key trying to score an agent outside their roster → 403 + denied row."""
    async def fake_download(call_id):
        return b"BYTES"

    monkeypatch.setattr(scoring_module, "download_recording", fake_download)

    resp = client.post(
        "/api/member_support/score",
        headers={"Authorization": f"Bearer {TEAM_TOKEN}"},
        data={
            "call_id": "call-foo",
            "agent_email": "stranger@landing.com",  # not in stubbed roster
            "manager_email": "ana@landing.com",
        },
    )
    assert resp.status_code == 403
    assert len(client.audit_rows) == 1
    assert client.audit_rows[0]["action"] == "denied"
    assert client.audit_rows[0]["api_key_role"] == "team"


def test_score_endpoint_privileged_bypasses_roster(client, monkeypatch):
    """Privileged key on unrostered agent → success, audit shows privileged role."""
    async def fake_download(call_id):
        return b"BYTES"

    monkeypatch.setattr(scoring_module, "download_recording", fake_download)

    resp = client.post(
        "/api/sales/score",
        headers={"Authorization": f"Bearer {PRIV_TOKEN}"},
        data={
            "call_id": "call-priv",
            "agent_email": "contractor@external.com",
            "agent_name": "External Contractor",
            "manager_email": "hr@landing.com",
        },
    )
    assert resp.status_code == 200, resp.text
    assert client.audit_rows[0]["api_key_role"] == "privileged"
    assert client.audit_rows[0]["target_team"] == "sales"


# ---------------------------------------------------------------------------
# 400 validation
# ---------------------------------------------------------------------------

def test_score_endpoint_rejects_missing_agent_identity(client):
    resp = client.post(
        "/api/member_support/score",
        headers={"Authorization": f"Bearer {TEAM_TOKEN}"},
        data={
            "call_id": "call-bare",
            "manager_email": "ana@landing.com",
        },
    )
    assert resp.status_code == 400
    assert "agent_email" in resp.json()["detail"]

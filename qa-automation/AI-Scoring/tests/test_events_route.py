"""/events/team/{team_id} SSE route — auth + wiring smoke tests.

Full streaming behavior isn't unit-testable cleanly with TestClient; the
EventBus pub/sub fanout is covered by `test_event_bus.py`. This file
asserts the things that ARE testable without a long-lived connection:
auth enforcement (both Authorization and ?api_key= paths) and that the
approve publish hook calls into the bus.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.middleware import auth
from backend.middleware.auth import KeyIdentity, TEAM_AUTH_DEPENDENCY
from backend.routes import events as events_module
from backend.routes import scoring as scoring_module


TEAM_TOKEN = "team-ms-tok"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(auth, "_KEY_MAP", {
        TEAM_TOKEN: KeyIdentity(role="team", team_id="member_support"),
    })

    # The real _stream is an infinite generator (heartbeats every 15s) and
    # TestClient context exit waits for the response to drain — so an
    # unmocked test hangs. Replace with a terminating one-chunk generator
    # for auth tests; the publish/fanout path is covered by test_event_bus.
    async def _short_stream(team_id):
        yield b": connected\n\n"

    monkeypatch.setattr(events_module, "_stream", _short_stream)

    app = FastAPI()
    app.include_router(
        events_module.router,
        prefix="/api/{team_id}",
        dependencies=TEAM_AUTH_DEPENDENCY,
    )
    return TestClient(app)


# ---------------------------------------------------------------------------
# Auth on /events
# ---------------------------------------------------------------------------

def test_events_endpoint_rejects_missing_auth(client):
    """No header AND no query param → 401 before the stream opens."""
    resp = client.get("/api/member_support/events/stream")
    assert resp.status_code == 401


def test_events_endpoint_rejects_wrong_team_token(client):
    """Team-scoped key for the WRONG team → 403."""
    # Register a sales-only key for this test.
    auth._KEY_MAP["sales-tok"] = KeyIdentity(role="team", team_id="sales")
    try:
        resp = client.get(
            "/api/member_support/events/stream",
            headers={"Authorization": "Bearer sales-tok"},
        )
        assert resp.status_code == 403
    finally:
        auth._KEY_MAP.pop("sales-tok", None)


def test_events_endpoint_accepts_query_param_auth(client):
    """EventSource cannot set headers — confirm ?api_key= path passes auth.

    The fixture replaces _stream with a one-chunk generator so the request
    terminates cleanly. We assert: auth passed, content-type is SSE, and
    the priming chunk reaches the client."""
    resp = client.get(
        f"/api/member_support/events/stream?api_key={TEAM_TOKEN}",
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert b": connected" in resp.content


# ---------------------------------------------------------------------------
# Approval-handler publish wiring
# ---------------------------------------------------------------------------

def test_approve_handler_calls_event_bus_publish():
    """The approve route must call get_event_bus().publish on success.

    Source-presence check rather than a full integration test — the
    approve pipeline runs five sheet-side stages, all of which need
    stubs that don't exist in this repo yet. The fanout itself is
    tested in test_event_bus.py."""
    src = inspect.getsource(scoring_module)
    assert "get_event_bus()" in src, (
        "approve handler no longer calls get_event_bus() — the SSE "
        "publish hook has regressed"
    )
    assert '"eval_approved"' in src, (
        "approve handler no longer publishes the 'eval_approved' event"
    )


def test_approve_publish_truncates_long_text():
    """The 280-char cap (LiveDashboard.md Q2) is implemented as a local
    _truncate function inside the approve handler. Source-presence
    check — fail if the cap is silently raised/lowered or removed."""
    src = inspect.getsource(scoring_module)
    assert "_truncate" in src, "_truncate helper is missing from scoring.py"
    assert "280" in src, "_truncate's 280-char default has been changed"


def test_approve_publish_includes_eval_id_field():
    """The published eval_approved payload must include `eval_id` derived
    from dialpad_link so the toast click navigates to the same datapoint
    URL the rest of the dashboard uses (entry_point_call_id, not master
    call_id). Source-presence guard against regression of the fix."""
    src = inspect.getsource(scoring_module)
    assert "_eval_id_from_link" in src, (
        "_eval_id_from_link helper is missing — approve publish would "
        "fall back to master call_id and the toast would link to the "
        "wrong datapoint URL"
    )
    assert '"eval_id": eval_id' in src, (
        "eval_id is no longer published in the eval_approved payload"
    )

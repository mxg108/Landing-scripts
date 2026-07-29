"""Webhook route → SSE bus: the agent_status publish seam.

The fold/store layers are covered by test_cc_fold.py and the
integration suite; this file asserts the route-level contract added
2026-07-29: a verified agent-status event publishes an `agent_status`
SSE event for the resolved team, dedupe redeliveries stay silent, and
call events never cross-publish. The bus is swapped for a recorder —
fanout mechanics live in test_event_bus.py.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import jwt as pyjwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from command_center.routes import webhooks as webhooks_module
from command_center.services import store

SECRET = "test-subscription-secret"

T0 = datetime(2026, 7, 29, 15, 0, 0, tzinfo=timezone.utc)
T0_MS = int(T0.timestamp() * 1000)


class _RecordingBus:
    def __init__(self) -> None:
        self.published: list[tuple[str, str, dict]] = []

    async def publish(self, team_id: str, event: str, payload: dict) -> None:
        self.published.append((team_id, event, payload))


def _signed(payload: dict) -> bytes:
    return pyjwt.encode(payload, SECRET, algorithm="HS256").encode("utf-8")


@pytest.fixture
def harness(monkeypatch):
    monkeypatch.setenv("DIALPAD_WEBHOOK_SECRET", SECRET)

    bus = _RecordingBus()
    monkeypatch.setattr(webhooks_module, "get_event_bus", lambda: bus)

    ingest_results: list[str] = ["appended"]

    async def _fake_ingest(team_id: str, payload: dict) -> str:
        return ingest_results[0]

    monkeypatch.setattr(store, "ingest_event", _fake_ingest)
    monkeypatch.setattr(webhooks_module.store, "ingest_event", _fake_ingest)

    app = FastAPI()
    app.include_router(webhooks_module.router)
    return TestClient(app), bus, ingest_results


def _agent_status_payload(**overrides) -> dict:
    payload = {
        "state": "available",
        "event_timestamp": T0_MS,
        "target": {"id": 9876543210, "type": "user", "name": "Ana López"},
    }
    payload.update(overrides)
    return payload


def test_agent_status_event_publishes_toast(harness):
    client, bus, _ = harness
    resp = client.post("/api/webhooks/dialpad", content=_signed(_agent_status_payload()))
    assert resp.status_code == 200
    assert resp.json() == {"status": "appended"}

    assert len(bus.published) == 1
    team_id, event, data = bus.published[0]
    # target 9876543210 maps to no configured id → single-team fallback.
    assert team_id == "member_support"
    assert event == "agent_status"
    assert data["agent"] == "Ana López"
    assert data["status"] == "available"
    assert data["agent_id"] == "9876543210"
    assert data["at"] == T0.isoformat()


def test_duplicate_delivery_does_not_republish(harness):
    client, bus, ingest_results = harness
    ingest_results[0] = "duplicate"
    resp = client.post("/api/webhooks/dialpad", content=_signed(_agent_status_payload()))
    assert resp.status_code == 200
    assert resp.json() == {"status": "duplicate"}
    assert bus.published == []


def test_call_event_never_publishes_agent_status(harness):
    client, bus, ingest_results = harness
    ingest_results[0] = "ingested"
    payload = {
        "state": "connected",
        "call_id": 555000111,
        "date_connected": T0_MS,
        "target": {"id": 4716644561813504, "type": "callcenter", "name": "MS"},
    }
    resp = client.post("/api/webhooks/dialpad", content=_signed(payload))
    assert resp.status_code == 200
    assert resp.json() == {"status": "ingested"}
    assert bus.published == []


def test_nameless_payload_still_publishes_with_placeholders(harness):
    client, bus, _ = harness
    payload = _agent_status_payload(target={"id": 42, "type": "user"})
    resp = client.post("/api/webhooks/dialpad", content=_signed(payload))
    assert resp.status_code == 200
    assert len(bus.published) == 1
    _, _, data = bus.published[0]
    assert data["agent"] == ""          # client renders 'Agent'
    assert data["status"] == "available"


def test_bad_signature_publishes_nothing(harness):
    client, bus, _ = harness
    body = pyjwt.encode(_agent_status_payload(), "wrong-secret", algorithm="HS256")
    resp = client.post("/api/webhooks/dialpad", content=body.encode("utf-8"))
    assert resp.status_code == 401
    assert bus.published == []

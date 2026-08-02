"""Dashboard one-pager serving — service orchestrator + route.

The render machinery moved into backend/services/onepager.py so the
dashboard can serve the artifact on demand (test_onepager.py covers the
render pieces). This file covers what the move added: last-closed-month
semantics (bucket TZ, not UTC), the month/agent slice in
render_month_onepager, the no-Gemini-on-page-view guarantee, and the
route contract (default month, override, 422/404, team-key auth).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.middleware import auth
from backend.middleware.auth import KeyIdentity, TEAM_AUTH_DEPENDENCY
from backend.routes import dashboard as dashboard_route
from backend.services import assessment_store, team_source
from backend.services.onepager import last_closed_month, render_month_onepager
from tests.conftest import load_test_config

_LA = ZoneInfo("America/Los_Angeles")


# ---------------------------------------------------------------------------
# last_closed_month
# ---------------------------------------------------------------------------

def test_last_closed_month_mid_month():
    assert last_closed_month(datetime(2026, 8, 14, 12, 0, tzinfo=_LA)) == "2026-07"


def test_last_closed_month_january_rolls_the_year():
    assert last_closed_month(datetime(2027, 1, 1, 6, 0, tzinfo=_LA)) == "2026-12"


def test_last_closed_month_is_bucket_tz_not_utc():
    # Aug 1, 06:00 UTC is still Jul 31 in LA — the closed month is June.
    assert last_closed_month(datetime(2026, 8, 1, 6, 0, tzinfo=timezone.utc)) == "2026-06"


# ---------------------------------------------------------------------------
# render_month_onepager
# ---------------------------------------------------------------------------

@pytest.fixture
def ms_config():
    return load_test_config("member_support")


def _frame(ms_config):
    """Minimal analytics frame: naive-UTC timestamps (the frame
    convention), two July evals for Ana, one for Bo."""
    numeric_id = next(
        sec.history_id for sec in ms_config.sections_by_number
        if sec.score_type in ("numeric", "manual")
    )
    df = pd.DataFrame({
        "agent": ["Ana", "Ana", "Bo"],
        "timestamp": [
            datetime(2026, 7, 10, 18, 0),
            datetime(2026, 7, 20, 21, 0),
            datetime(2026, 7, 12, 17, 0),
        ],
        "overall_score": [88.0, 92.0, 70.0],
    })
    df[numeric_id] = [4, 5, 3]
    return df


@pytest.fixture
def patched_sources(ms_config, monkeypatch):
    """Fake the frame + assessment sources; capture the generate flag."""
    captured = {}

    async def fake_frame(config):
        return _frame(ms_config)

    async def fake_assessment(config, agent, month, generate=True):
        captured["generate"] = generate
        return None

    monkeypatch.setattr(team_source, "fetch_history_frame", fake_frame)
    monkeypatch.setattr(
        assessment_store, "get_or_generate_month_assessment", fake_assessment)
    return captured


def test_renders_month_slice(ms_config, patched_sources):
    page = asyncio.run(render_month_onepager(ms_config, "Ana", "2026-07"))
    assert page is not None
    assert "Ana" in page
    assert "July 2026" in page
    assert ms_config.display_name in page


def test_page_view_never_generates(ms_config, patched_sources):
    asyncio.run(render_month_onepager(ms_config, "Ana", "2026-07"))
    assert patched_sources["generate"] is False


def test_cli_generate_flag_passes_through(ms_config, patched_sources):
    asyncio.run(render_month_onepager(ms_config, "Ana", "2026-07", generate=True))
    assert patched_sources["generate"] is True


def test_none_when_agent_has_no_rows_in_month(ms_config, patched_sources):
    assert asyncio.run(render_month_onepager(ms_config, "Ana", "2026-06")) is None
    assert asyncio.run(render_month_onepager(ms_config, "Nadie", "2026-07")) is None


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------

MS_TOKEN = "team-ms-tok"
SALES_TOKEN = "team-sales-tok"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(auth, "_KEY_MAP", {
        MS_TOKEN: KeyIdentity(role="team", team_id="member_support"),
        SALES_TOKEN: KeyIdentity(role="team", team_id="sales"),
    })
    app = FastAPI()
    app.include_router(
        dashboard_route.router,
        prefix="/api/{team_id}",
        dependencies=TEAM_AUTH_DEPENDENCY,
    )
    return TestClient(app)


def _get(client, team, name, token, month=None):
    return client.get(
        f"/api/{team}/agents/{name}/onepager"
        + (f"?month={month}" if month else ""),
        headers={"Authorization": f"Bearer {token}"},
    )


def test_route_serves_html_with_default_month(client, monkeypatch):
    async def fake_render(config, agent, month, **kwargs):
        return f"<!DOCTYPE html><html><body>{agent} {month}</body></html>"
    monkeypatch.setattr(dashboard_route, "render_month_onepager", fake_render)
    monkeypatch.setattr(dashboard_route, "last_closed_month", lambda: "2026-07")
    resp = _get(client, "member_support", "Ana", MS_TOKEN)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "Ana 2026-07" in resp.text  # default month = last closed


def test_route_month_override(client, monkeypatch):
    async def fake_render(config, agent, month, **kwargs):
        return f"<html>{month}</html>"
    monkeypatch.setattr(dashboard_route, "render_month_onepager", fake_render)
    resp = _get(client, "member_support", "Ana", MS_TOKEN, month="2026-05")
    assert resp.status_code == 200
    assert "2026-05" in resp.text


def test_route_rejects_malformed_month(client):
    for bad in ("2026-13", "junk", "2026-7", "2026-07-01"):
        resp = _get(client, "member_support", "Ana", MS_TOKEN, month=bad)
        assert resp.status_code == 422, bad


def test_route_404s_when_no_rows(client, monkeypatch):
    async def fake_render(config, agent, month, **kwargs):
        return None
    monkeypatch.setattr(dashboard_route, "render_month_onepager", fake_render)
    resp = _get(client, "member_support", "Ana", MS_TOKEN, month="2026-07")
    assert resp.status_code == 404


def test_route_requires_matching_team_key(client):
    resp = _get(client, "member_support", "Ana", SALES_TOKEN, month="2026-07")
    assert resp.status_code in (401, 403)

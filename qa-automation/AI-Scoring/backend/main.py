"""FastAPI application entry point."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from dotenv import load_dotenv
import os

# Load .env from the AI-Scoring directory regardless of where uvicorn is launched
_env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_env_path)

# Uvicorn only configures its own `uvicorn.*` loggers; without this, every
# INFO line from backend.* (version-ship outcomes, cutover shadow compares,
# eval_store finalizations) falls through to an unconfigured root logger and
# is dropped. basicConfig is a no-op when a root handler already exists
# (e.g. under pytest), so this never double-logs.
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)

# Command Center mount (LandingOpsCommandCenter.md §6): command_center/
# lives INSIDE this subtree as a sibling of backend/ — moved from the
# repo root 2026-07-29. The Railway service builds from
# qa-automation/AI-Scoring (service Root Directory), so the old
# top-level location was never in the image: the import failed and the
# app booted without /api/webhooks/dialpad, 404ing every Dialpad
# delivery. In-tree, absence is a build defect — the import is
# unconditional so breakage fails the boot instead of silently dropping
# webhook ingress.
import sys
_app_root = Path(__file__).resolve().parent.parent
if str(_app_root) not in sys.path:
    sys.path.insert(0, str(_app_root))

from command_center.routes.webhooks import router as cc_webhooks_router
from command_center.services.store import close_pool as cc_close_pool

from backend.services.version_ship import run_startup_ship
from backend.routes.scoring import router as scoring_router
from backend.routes.dashboard import router as dashboard_router
from backend.routes.team import router as team_router
from backend.routes.datapoints import router as datapoints_router
from backend.routes.lookup import router as lookup_router
from backend.routes.events import router as events_router
from backend.routes.hr_bonus import router as hr_bonus_router
from backend.middleware.auth import AUTH_DEPENDENCY, TEAM_AUTH_DEPENDENCY
from backend.middleware.audit import AuditLogMiddleware

@asynccontextmanager
async def _lifespan(app: FastAPI):
    # §3.12 formula-version ship: validate config/scoring/*/overall_formula.json
    # (always — formula bugs are startup failures) and archive new versions in
    # qa.formula_versions when a DB is configured. "Drop a revised JSON +
    # restart" is the whole ship ceremony; see services/version_ship.py.
    await run_startup_ship(
        os.environ.get("DATABASE_URL"),
        strict=os.environ.get("QA_VERSION_SHIP_STRICT", "") == "1",
    )
    # Interim CC ingestion: Stats-API dispositions pull on a loop, gated
    # by CC_STATS_PULL_INTERVAL_MIN (Railway env). Off when unset — local
    # dev and tests never hit the Stats API.
    import asyncio
    from backend.services import disposition_pull
    _pull_task = None
    _pull_interval = disposition_pull.periodic_interval_minutes()
    if _pull_interval is not None:
        _pull_task = asyncio.create_task(
            disposition_pull.run_periodic_pull(_pull_interval)
        )
    yield
    if _pull_task is not None:
        _pull_task.cancel()
        try:
            await _pull_task
        except asyncio.CancelledError:
            pass
    # Close the PostgresProvider pools built by the get_provider factory.
    from backend.services.data_provider import close_all_providers
    await close_all_providers()
    # Wave 2 Phase 4a: dual-write pool (lazy-created on first Stage 1 write).
    from backend.services.eval_store import close_pool
    await close_pool()
    # Command Center webhook-ingest pool (lazy-created on first event).
    await cc_close_pool()
    # RAG provider (Pulpo MCP session; lazy-created on first retrieval).
    from backend.services.rag.factory import close_rag_provider
    await close_rag_provider()


app = FastAPI(
    title="Landing QA Scoring API",
    description="AI-powered call center QA scoring pipeline",
    version="1.2.0",
    lifespan=_lifespan,
)

_allowed_origins = [
    o.strip()
    for o in os.getenv("ALLOWED_ORIGINS", "http://localhost:8000").split(",")
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(AuditLogMiddleware)

# Team-aware routes (primary). API key's team_id must match the URL team_id.
for r in (scoring_router, dashboard_router, team_router, datapoints_router, lookup_router, events_router, hr_bonus_router):
    app.include_router(r, prefix="/api/{team_id}", dependencies=TEAM_AUTH_DEPENDENCY)

# Legacy single-team shim (30-day transition). team_id defaults to 'member_support'.
# Events + hr-bonus routers intentionally excluded — both are new and team-scoped only.
for r in (scoring_router, dashboard_router, team_router, datapoints_router, lookup_router):
    app.include_router(r, prefix="/api", dependencies=AUTH_DEPENDENCY)


# Command Center webhook ingress — NO API-key dependency: Dialpad is the
# caller and the JWT body signature is the authentication (DispositionDesign
# §4). Distinct URL space from the QA routers above.
app.include_router(cc_webhooks_router)


@app.get("/api/health")
async def health():
    return {"status": "ok"}

# Serve the frontend HTML at the root
_frontend_dir = Path(__file__).resolve().parent.parent / "frontend"

# Shared CSS/JS for the persistent top-bar (header.css, header.js) lives
# under frontend/static/. Mounted here so both team_dashboard.html and
# dashboard.html can <link>/<script> from a single source of truth.
app.mount("/static", StaticFiles(directory=_frontend_dir / "static"), name="static")

# The HTML pages embed all JS/CSS inline, so a stale HTML response means
# a stale app. no-cache forces the browser to revalidate on every reload
# (an ETag round-trip, not a full re-download) so frontend edits surface
# immediately without manual hard-refreshes. Static API responses are
# unaffected.
_HTML_NO_CACHE = {"Cache-Control": "no-cache"}


# --- Team-aware page routes ---

@app.get("/score/{team_id}", include_in_schema=False)
async def serve_scoring_ui(team_id: str):
    return FileResponse(_frontend_dir / "index.html", headers=_HTML_NO_CACHE)


@app.get("/dashboard/{team_id}/agent/{name:path}", include_in_schema=False)
async def serve_agent_dashboard(team_id: str, name: str):
    return FileResponse(_frontend_dir / "dashboard.html", headers=_HTML_NO_CACHE)


@app.get("/datapoint/{team_id}/{call_id}", include_in_schema=False)
async def serve_datapoint_page(team_id: str, call_id: str):
    return FileResponse(_frontend_dir / "datapoint.html", headers=_HTML_NO_CACHE)


@app.get("/dashboard/{team_id}", include_in_schema=False)
async def serve_team_dashboard(team_id: str):
    return FileResponse(_frontend_dir / "team_dashboard.html", headers=_HTML_NO_CACHE)


@app.get("/dashboard/{team_id}/evals", include_in_schema=False)
async def serve_team_evals_page(team_id: str):
    return FileResponse(_frontend_dir / "team_evals.html", headers=_HTML_NO_CACHE)


@app.get("/lookup/{team_id}", include_in_schema=False)
async def serve_lookup_page(team_id: str):
    return FileResponse(_frontend_dir / "lookup.html", headers=_HTML_NO_CACHE)


@app.get("/scorecard/{team_id}/{job_id}", include_in_schema=False)
async def serve_scorecard_page(team_id: str, job_id: str):
    return FileResponse(_frontend_dir / "scorecard.html", headers=_HTML_NO_CACHE)


# --- Legacy page redirects (30-day transition) ---

@app.get("/", include_in_schema=False)
async def serve_frontend():
    return RedirectResponse(url="/score/member_support", status_code=307)


@app.get("/dashboard", include_in_schema=False)
async def legacy_team_dashboard():
    return RedirectResponse(url="/dashboard/member_support", status_code=307)


@app.get("/dashboard/agent/{name:path}", include_in_schema=False)
async def legacy_agent_dashboard(name: str):
    return RedirectResponse(url=f"/dashboard/member_support/agent/{name}", status_code=307)


@app.get("/datapoint/{call_id}", include_in_schema=False)
async def legacy_datapoint(call_id: str):
    return RedirectResponse(url=f"/datapoint/member_support/{call_id}", status_code=307)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.main:app", host="0.0.0.0", port=8000, reload=True)

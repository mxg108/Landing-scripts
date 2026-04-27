"""FastAPI application entry point."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from pathlib import Path
from dotenv import load_dotenv
import os

# Load .env from the AI-Scoring directory regardless of where uvicorn is launched
_env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_env_path)

from backend.routes.scoring import router as scoring_router
from backend.routes.dashboard import router as dashboard_router
from backend.routes.team import router as team_router
from backend.routes.datapoints import router as datapoints_router
from backend.routes.lookup import router as lookup_router
from backend.middleware.auth import AUTH_DEPENDENCY, TEAM_AUTH_DEPENDENCY
from backend.middleware.audit import AuditLogMiddleware

app = FastAPI(
    title="Landing QA Scoring API",
    description="AI-powered call center QA scoring pipeline",
    version="1.2.0",
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
for r in (scoring_router, dashboard_router, team_router, datapoints_router, lookup_router):
    app.include_router(r, prefix="/api/{team_id}", dependencies=TEAM_AUTH_DEPENDENCY)

# Legacy single-team shim (30-day transition). team_id defaults to 'member_support'.
for r in (scoring_router, dashboard_router, team_router, datapoints_router, lookup_router):
    app.include_router(r, prefix="/api", dependencies=AUTH_DEPENDENCY)


@app.get("/api/health")
async def health():
    return {"status": "ok"}

# Serve the frontend HTML at the root
_frontend_dir = Path(__file__).resolve().parent.parent / "frontend"


# --- Team-aware page routes ---

@app.get("/score/{team_id}", include_in_schema=False)
async def serve_scoring_ui(team_id: str):
    return FileResponse(_frontend_dir / "index.html")


@app.get("/dashboard/{team_id}/agent/{name:path}", include_in_schema=False)
async def serve_agent_dashboard(team_id: str, name: str):
    return FileResponse(_frontend_dir / "dashboard.html")


@app.get("/datapoint/{team_id}/{call_id}", include_in_schema=False)
async def serve_datapoint_page(team_id: str, call_id: str):
    return FileResponse(_frontend_dir / "datapoint.html")


@app.get("/dashboard/{team_id}", include_in_schema=False)
async def serve_team_dashboard(team_id: str):
    return FileResponse(_frontend_dir / "team_dashboard.html")


@app.get("/lookup/{team_id}", include_in_schema=False)
async def serve_lookup_page(team_id: str):
    return FileResponse(_frontend_dir / "lookup.html")


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

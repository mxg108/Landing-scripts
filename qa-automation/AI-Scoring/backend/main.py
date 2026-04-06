"""FastAPI application entry point."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pathlib import Path
from dotenv import load_dotenv
import os

# Load .env from the AI-Scoring directory regardless of where uvicorn is launched
_env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_env_path)

from backend.routes.scoring import router
from backend.routes.dashboard import router as dashboard_router
from backend.routes.team import router as team_router
from backend.middleware.auth import AUTH_DEPENDENCY
from backend.middleware.audit import AuditLogMiddleware

app = FastAPI(
    title="Landing QA Scoring API",
    description="AI-powered call center QA scoring pipeline",
    version="1.1.0",
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

app.include_router(router, dependencies=AUTH_DEPENDENCY)
app.include_router(dashboard_router, dependencies=AUTH_DEPENDENCY)
app.include_router(team_router, dependencies=AUTH_DEPENDENCY)


@app.get("/api/health")
async def health():
    return {"status": "ok"}

# Serve the frontend HTML at the root
_frontend_dir = Path(__file__).resolve().parent.parent / "frontend"

@app.get("/", include_in_schema=False)
async def serve_frontend():
    return FileResponse(_frontend_dir / "index.html")


# Agent drill-down — must be registered before /dashboard to avoid path conflicts
@app.get("/dashboard/agent/{name:path}", include_in_schema=False)
async def serve_agent_dashboard(name: str):
    return FileResponse(_frontend_dir / "dashboard.html")


@app.get("/dashboard", include_in_schema=False)
async def serve_team_dashboard():
    return FileResponse(_frontend_dir / "team_dashboard.html")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.main:app", host="0.0.0.0", port=8000, reload=True)

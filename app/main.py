"""FastAPI application — the control plane. It exposes REST + webhooks + the
scenario simulator and serves the browser WebRTC demo client. It does NOT stream
audio frames; the Pipecat agent process (app/agent) handles realtime media."""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import (
    assessment, calls, candidates, health, interviews, observability, rag, scenarios,
)
from app.config import settings
from app.db.session import init_db
# Importing tracing configures LangSmith env from settings (no-op without a key).
from app.observability.tracing import TRACING_ENABLED, trace

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Inbound Interview Agent", version="0.1.0", lifespan=lifespan)


@app.middleware("http")
async def _langsmith_request_trace(request: Request, call_next):
    """One LangSmith trace tree per request; all traced steps nest under it.
    contextvars propagate into the sync-endpoint threadpool, so nesting works."""
    if not TRACING_ENABLED or request.url.path in ("/health", "/"):
        return await call_next(request)
    with trace(
        name=f"{request.method} {request.url.path}",
        run_type="chain",
        project_name=settings.langsmith_project,
        metadata={"path": request.url.path, "method": request.method},
    ) as run:
        response = await call_next(request)
        try:
            run.add_outputs({"status_code": response.status_code})
        except Exception:
            pass
        return response

app.include_router(health.router)
app.include_router(scenarios.router)
app.include_router(candidates.router)
app.include_router(interviews.router)
app.include_router(calls.router)
app.include_router(rag.router)
app.include_router(observability.router)
app.include_router(assessment.router)

if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")

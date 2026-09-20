"""Health, liveness, and readiness endpoints."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from sqlalchemy import text

from app.api.observability import grading_stats
from app.config import settings
from app.db.session import session_scope

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict:
    """Legacy health endpoint — shows provider config."""
    return {
        "status": "ok",
        "transport": settings.transport_provider,
        "stt": settings.stt_provider,
        "llm": settings.llm_provider,
        "tts": settings.tts_provider,
        "groq_key_present": bool(settings.groq_api_key),
    }


@router.get("/live")
def live() -> dict:
    """Liveness: is the process alive? Always returns 200 if the endpoint responds.
    No DB touch — this is a minimal probe for orchestrators."""
    return {"status": "ok"}


@router.get("/ready")
def ready() -> dict:
    """Readiness: can the process handle requests? Checks DB connectivity,
    includes grading backlog health as a non-fatal signal. Returns 503 if
    the DB check fails.

    This is an unauthenticated probe (no Depends(require_*) — orchestrators need to
    hit it before any credentials are wired up), so the failure detail must never
    include the exception's own string: a connection-level SQLAlchemy/psycopg error
    can render the DSN (including the password) into its message. Only the exception
    TYPE NAME is safe to return — same reasoning as assessment_service._grade's
    provider-error handling."""
    # `SET LOCAL statement_timeout` bounds the query once a connection exists — the
    # failure mode this project has actually hit (lock contention). It does NOT bound
    # the initial TCP connect itself (a true network partition to `postgres` could still
    # hang); closing that fully would need connect_timeout in the engine's own
    # connect_args, a bigger, engine-wide change not made here.
    try:
        with session_scope() as session:
            session.execute(text("SET LOCAL statement_timeout = '2s'"))
            session.execute(text("SELECT 1"))
    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail=f"Database connectivity check failed: {type(e).__name__}"
        )

    # Include grading backlog health as an informational signal.
    # A large backlog doesn't fail readiness (the API can still serve requests),
    # but we expose it for monitoring.
    try:
        grading_stats_data = grading_stats()
    except Exception:
        # If the grading stats query fails, still return 200 — the API itself
        # is working even if observability is broken.
        grading_stats_data = {"backlog": None, "oldest_pending_age_seconds": None}

    return {
        "status": "ready",
        "database": "ok",
        "grading_backlog": grading_stats_data.get("backlog"),
        "oldest_pending_age_seconds": grading_stats_data.get("oldest_pending_age_seconds"),
    }

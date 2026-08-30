"""Observability endpoints — query LangSmith traces via the current SDK API."""
from __future__ import annotations

from fastapi import APIRouter

from app.observability import query

router = APIRouter(prefix="/observability", tags=["observability"])


@router.get("/summary")
async def summary(limit: int = 200) -> dict:
    """Token usage, errors, and latency by run type over recent traces."""
    return await query.summarize_runs(limit=limit)


@router.get("/runs")
async def runs(limit: int = 50) -> dict:
    """Most recent traced runs (id, name, type, tokens, error, latency)."""
    return {"runs": await query.recent_runs(limit=limit)}

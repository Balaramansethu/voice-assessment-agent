"""Observability endpoints — query LangSmith traces via the current SDK API."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func, select

from app.api.auth import require_ops
from app.db.models import GradingJob
from app.db.session import session_scope
from app.observability import query

router = APIRouter(prefix="/observability", tags=["observability"],
                   dependencies=[Depends(require_ops)])


@router.get("/summary")
async def summary(limit: int = 200) -> dict:
    """Token usage, errors, and latency by run type over recent traces."""
    return await query.summarize_runs(limit=limit)


@router.get("/runs")
async def runs(limit: int = 50) -> dict:
    """Most recent traced runs (id, name, type, tokens, error, latency)."""
    return {"runs": await query.recent_runs(limit=limit)}


@router.get("/grading")
def grading_stats(call_id: int | None = None) -> dict:
    """Grading-job backlog/health for the durable queue (P3).
    PR-509: optional call_id parameter filters to just that call's jobs."""
    with session_scope() as session:
        where_clause = []
        if call_id is not None:
            where_clause.append(GradingJob.call_id == call_id)

        counts = dict(session.execute(
            select(GradingJob.status, func.count()).where(*where_clause)
            .group_by(GradingJob.status)
        ).all())
        oldest_pending = session.scalar(
            select(func.min(GradingJob.next_attempt_at))
            .where(GradingJob.status.in_(("PENDING", "RETRY")), *where_clause)
        )
        avg_duration = session.scalar(
            select(func.avg(GradingJob.updated_at - GradingJob.created_at))
            .where(GradingJob.status.in_(("COMPLETE", "FAILED")), *where_clause)
        )
    now = datetime.now(timezone.utc)
    return {
        "backlog": counts.get("PENDING", 0) + counts.get("RETRY", 0),
        "running": counts.get("RUNNING", 0),
        "retry_count": counts.get("RETRY", 0),
        "failed_count": counts.get("FAILED", 0),
        "complete_count": counts.get("COMPLETE", 0),
        "oldest_pending_age_seconds": (now - oldest_pending).total_seconds() if oldest_pending else None,
        "avg_processing_seconds": avg_duration.total_seconds() if avg_duration else None,
    }

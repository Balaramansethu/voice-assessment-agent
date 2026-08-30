"""Query LangSmith traces via the current (non-deprecated) runs-query API.

The deprecated `client.list_runs()` is replaced by the `POST /api/v1/runs/query`
endpoint (the same one the new `client.runs.query()` wraps). We call it directly
with httpx because the 0.11.1 SDK wrapper has a timeout-arithmetic bug; the REST
endpoint itself is the supported, non-deprecated surface. Read-only.
"""
from __future__ import annotations

from datetime import datetime

import httpx

from app.config import settings
from app.observability.tracing import TRACING_ENABLED

_BASE = settings.langsmith_endpoint.rstrip("/")
_HEADERS = {"x-api-key": settings.langsmith_api_key}


async def _project_id(client: httpx.AsyncClient) -> str | None:
    r = await client.get(f"{_BASE}/api/v1/sessions",
                         params={"name": settings.langsmith_project, "limit": 1},
                         headers=_HEADERS)
    r.raise_for_status()
    data = r.json()
    return data[0]["id"] if data else None


def _latency_ms(run: dict) -> int | None:
    s, e = run.get("start_time"), run.get("end_time")
    if not s or not e:
        return None
    try:
        ds = datetime.fromisoformat(s.replace("Z", "+00:00"))
        de = datetime.fromisoformat(e.replace("Z", "+00:00"))
        return int((de - ds).total_seconds() * 1000)
    except (ValueError, AttributeError):
        return None


async def recent_runs(limit: int = 100) -> list[dict]:
    """Most recent runs in the project (default window: last day) as plain dicts."""
    if not TRACING_ENABLED:
        return []
    async with httpx.AsyncClient(timeout=20) as client:
        pid = await _project_id(client)
        if not pid:
            return []
        r = await client.post(
            f"{_BASE}/api/v1/runs/query",
            headers=_HEADERS,
            json={"session": [pid], "limit": min(limit, 100),
                  "select": ["name", "run_type", "status", "total_tokens",
                             "error", "start_time", "end_time"]},
        )
        r.raise_for_status()
        runs = r.json().get("runs", [])
        return [{
            "id": run.get("id"),
            "name": run.get("name"),
            "run_type": run.get("run_type"),
            "status": run.get("status"),
            "total_tokens": run.get("total_tokens"),
            "error": bool(run.get("error")),
            "latency_ms": _latency_ms(run),
        } for run in runs]


async def summarize_runs(limit: int = 200) -> dict:
    """Aggregate recent runs: per-type counts, tokens, errors, avg latency."""
    if not TRACING_ENABLED:
        return {"tracing_enabled": False,
                "note": "Set langsmith_tracing + langsmith_api_key to enable."}

    runs = await recent_runs(limit=limit)
    by_type: dict[str, dict] = {}
    total_tokens = 0
    total_errors = 0
    for r in runs:
        t = r["run_type"] or "unknown"
        b = by_type.setdefault(t, {"count": 0, "tokens": 0, "errors": 0,
                                   "_lat_sum": 0, "_lat_n": 0})
        b["count"] += 1
        b["tokens"] += r["total_tokens"] or 0
        b["errors"] += 1 if r["error"] else 0
        if r["latency_ms"] is not None:
            b["_lat_sum"] += r["latency_ms"]
            b["_lat_n"] += 1
        total_tokens += r["total_tokens"] or 0
        total_errors += 1 if r["error"] else 0

    for b in by_type.values():
        b["avg_latency_ms"] = round(b["_lat_sum"] / b["_lat_n"], 1) if b["_lat_n"] else None
        del b["_lat_sum"], b["_lat_n"]

    return {
        "tracing_enabled": True,
        "project": settings.langsmith_project,
        "runs_examined": len(runs),
        "total_tokens": total_tokens,
        "total_errors": total_errors,
        "by_run_type": by_type,
    }

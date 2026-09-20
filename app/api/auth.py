"""Shared-secret scope gates (PR-105) — matches this project's existing lightweight
auth patterns (Twilio HMAC check, Postgres-backed voice-session token): one static,
rotatable secret per scope, compared in constant time. No OAuth/JWT/session store."""
from __future__ import annotations

import hmac

from fastapi import Header, HTTPException

from app.config import settings


def _check(value: str | None, expected: str, header_name: str) -> None:
    if not expected:
        raise HTTPException(status_code=503, detail=f"{header_name} not configured")
    if value is None or not hmac.compare_digest(value, expected):
        raise HTTPException(status_code=401, detail="unauthorized")


def require_agent(x_agent_key: str | None = Header(default=None)) -> None:
    _check(x_agent_key, settings.agent_shared_key, "X-Agent-Key")


def require_recruiter(x_recruiter_key: str | None = Header(default=None)) -> None:
    _check(x_recruiter_key, settings.recruiter_shared_key, "X-Recruiter-Key")


def require_worker(x_worker_key: str | None = Header(default=None)) -> None:
    """Unused by any endpoint in this pass — reserved for P3's grading-worker
    claim/complete endpoints. Defined now per PR-105's literal requirement."""
    _check(x_worker_key, settings.worker_shared_key, "X-Worker-Key")


def require_ops(x_ops_key: str | None = Header(default=None)) -> None:
    _check(x_ops_key, settings.ops_shared_key, "X-Ops-Key")


def require_agent_or_recruiter(
    x_agent_key: str | None = Header(default=None),
    x_recruiter_key: str | None = Header(default=None),
) -> None:
    """The one endpoint (GET /assessment/by_call) two scopes legitimately share —
    a narrow hand-written combinator, not a general scope hierarchy."""
    agent_ok = bool(settings.agent_shared_key) and x_agent_key is not None and \
        hmac.compare_digest(x_agent_key, settings.agent_shared_key)
    recruiter_ok = bool(settings.recruiter_shared_key) and x_recruiter_key is not None and \
        hmac.compare_digest(x_recruiter_key, settings.recruiter_shared_key)
    if not (agent_ok or recruiter_ok):
        raise HTTPException(status_code=401, detail="unauthorized")

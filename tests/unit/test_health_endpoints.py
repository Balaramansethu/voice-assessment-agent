"""Liveness/readiness endpoints (PR-605). No DB touch for /live; /ready checks DB
connectivity and must never leak the raw exception string (a connection-level
SQLAlchemy/psycopg error can render the DSN, including the password)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.api import health as health_module

client = TestClient(main_module.app)


def test_live_always_ok():
    resp = client.get("/live")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_ready_ok_against_real_db():
    resp = client.get("/ready")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    assert body["database"] == "ok"
    assert "grading_backlog" in body
    assert "oldest_pending_age_seconds" in body


def test_ready_503_on_db_failure_without_leaking_exception_string(monkeypatch):
    class _BoomSession:
        def __enter__(self):
            raise RuntimeError("connection to server at postgresql://interview:s3cr3t@host failed")

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(health_module, "session_scope", lambda: _BoomSession())
    resp = client.get("/ready")
    assert resp.status_code == 503
    detail = resp.json()["detail"]
    assert "RuntimeError" in detail
    # The raw exception string (which could carry a DSN/password) must never appear.
    assert "s3cr3t" not in detail
    assert "postgresql://" not in detail


def test_ready_tolerates_grading_stats_failure(monkeypatch):
    """A broken /observability/grading query must not take down /ready — the API
    itself being reachable is what readiness means, not the grading queue's health."""
    def _boom():
        raise RuntimeError("grading_job table missing")

    monkeypatch.setattr(health_module, "grading_stats", _boom)
    resp = client.get("/ready")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    assert body["grading_backlog"] is None

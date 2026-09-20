"""Scope-gate unit tests (PR-105/108) — no DB needed."""
import pytest
from fastapi import HTTPException

from app.api.auth import require_agent, require_recruiter
from app.config import settings


def test_require_agent_rejects_missing_header(monkeypatch):
    monkeypatch.setattr(settings, "agent_shared_key", "real-key")
    with pytest.raises(HTTPException) as exc:
        require_agent(x_agent_key=None)
    assert exc.value.status_code == 401


def test_require_agent_rejects_wrong_key(monkeypatch):
    monkeypatch.setattr(settings, "agent_shared_key", "real-key")
    with pytest.raises(HTTPException) as exc:
        require_agent(x_agent_key="wrong-key")
    assert exc.value.status_code == 401


def test_require_agent_accepts_correct_key(monkeypatch):
    monkeypatch.setattr(settings, "agent_shared_key", "real-key")
    require_agent(x_agent_key="real-key")  # must not raise


def test_scopes_do_not_inherit(monkeypatch):
    monkeypatch.setattr(settings, "agent_shared_key", "agent-key")
    monkeypatch.setattr(settings, "recruiter_shared_key", "recruiter-key")
    with pytest.raises(HTTPException) as exc:
        require_recruiter(x_recruiter_key="agent-key")
    assert exc.value.status_code == 401


def test_scope_fails_closed_when_secret_unconfigured(monkeypatch):
    monkeypatch.setattr(settings, "agent_shared_key", "")
    with pytest.raises(HTTPException) as exc:
        require_agent(x_agent_key="anything")
    assert exc.value.status_code == 503


def test_production_config_rejects_dev_default_scope_keys(monkeypatch):
    from app.config import ConfigurationError, Settings, validate_production_config
    s = Settings(
        app_env="production",
        database_url="postgresql+psycopg://real:S3cure@prod-db:5432/interview",
        groq_api_key="gsk_real", deepgram_api_key="real",
        agent_shared_key="dev-agent-key-change-me",
        recruiter_shared_key="real-recruiter-key",
        worker_shared_key="real-worker-key",
        ops_shared_key="real-ops-key",
    )
    with pytest.raises(ConfigurationError, match="AGENT_SHARED_KEY"):
        validate_production_config(s)


def test_production_config_rejects_duplicate_scope_keys():
    from app.config import ConfigurationError, Settings, validate_production_config
    s = Settings(
        app_env="production",
        database_url="postgresql+psycopg://real:S3cure@prod-db:5432/interview",
        groq_api_key="gsk_real", deepgram_api_key="real",
        agent_shared_key="same-value",
        recruiter_shared_key="same-value",
        worker_shared_key="real-worker-key",
        ops_shared_key="real-ops-key",
    )
    with pytest.raises(ConfigurationError, match="must not reuse"):
        validate_production_config(s)

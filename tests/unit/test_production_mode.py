"""Environment-mode gating (PR-009) and startup fail-closed validation (PR-012).

No importlib.reload anywhere in this file. app.config.settings is a module-level
singleton shared by every module that imports it. Mutating an attribute on that live
object — via monkeypatch.setattr, never monkeypatch.setenv — is immediately visible
everywhere, with automatic teardown after each test.
"""
import pathlib

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.config import ConfigurationError, Settings, settings, validate_production_config

client = TestClient(main_module.app)


# ---- PR-009: scenario-route production lockout ----

def test_scenario_routes_mounted_outside_production(monkeypatch):
    monkeypatch.setattr(settings, "app_env", "development")
    resp = client.post("/scenarios/interrupted", json={})
    assert resp.status_code != 404


def test_scenario_routes_404_in_production(monkeypatch):
    monkeypatch.setattr(settings, "app_env", "production")
    resp = client.post("/scenarios/interrupted", json={})
    assert resp.status_code == 404


@pytest.mark.parametrize("value", ["Production", "PRODUCTION", "  production  ", "production\n"])
def test_scenario_routes_404_on_case_and_whitespace_variants(monkeypatch, value):
    monkeypatch.setattr(settings, "app_env", value)
    resp = client.post("/scenarios/interrupted", json={})
    assert resp.status_code == 404


def test_scenario_routes_restored_after_toggling_back(monkeypatch):
    monkeypatch.setattr(settings, "app_env", "production")
    assert client.post("/scenarios/interrupted", json={}).status_code == 404
    monkeypatch.setattr(settings, "app_env", "development")
    assert client.post("/scenarios/interrupted", json={}).status_code != 404


# ---- PR-012: validate_production_config (isolated function) ----

def test_validate_production_config_noop_outside_production():
    s = Settings(app_env="development")
    validate_production_config(s)


def test_validate_production_config_rejects_dev_database_url():
    s = Settings(
        app_env="production",
        database_url="postgresql+psycopg://interview:interview@postgres:5432/interview",
        groq_api_key="gsk_real", deepgram_api_key="real",
    )
    with pytest.raises(ConfigurationError, match="DATABASE_URL"):
        validate_production_config(s)


def test_validate_production_config_rejects_blank_database_credentials():
    s = Settings(
        app_env="production",
        database_url="postgresql+psycopg://:@postgres:5432/interview",
        groq_api_key="gsk_real", deepgram_api_key="real",
    )
    with pytest.raises(ConfigurationError, match="missing a username or password"):
        validate_production_config(s)


def test_validate_production_config_rejects_whitespace_only_database_credentials():
    """A URL-encoded space (%20) for username/password decodes to a non-empty-but-
    meaningless string — the blank check must strip before testing falsiness, or a
    stray-whitespace .env value would silently bypass fail-closed startup."""
    s = Settings(
        app_env="production",
        database_url="postgresql+psycopg://%20:%20@postgres:5432/interview",
        groq_api_key="gsk_real", deepgram_api_key="real",
    )
    with pytest.raises(ConfigurationError, match="missing a username or password"):
        validate_production_config(s)


def test_validate_production_config_rejects_dev_credentials_on_a_different_host():
    s = Settings(
        app_env="production",
        database_url="postgresql+psycopg://interview:interview@prod-db.example.com:5432/interview",
        groq_api_key="gsk_real", deepgram_api_key="real",
    )
    with pytest.raises(ConfigurationError, match="dev-default credentials"):
        validate_production_config(s)


def test_validate_production_config_accepts_real_credentials_on_the_dev_host():
    s = Settings(
        app_env="production",
        database_url="postgresql+psycopg://produser:S3cureP@ss@postgres:5432/interview",
        groq_api_key="gsk_real", deepgram_api_key="real",
        agent_shared_key="real-agent-key-1",
        recruiter_shared_key="real-recruiter-key-2",
        worker_shared_key="real-worker-key-3",
        ops_shared_key="real-ops-key-4",
    )
    validate_production_config(s)


def test_validate_production_config_rejects_malformed_database_url():
    s = Settings(app_env="production", database_url="not a url at all",
                 groq_api_key="gsk_real", deepgram_api_key="real",
                 agent_shared_key="real-key-1",
                 recruiter_shared_key="real-key-2",
                 worker_shared_key="real-key-3",
                 ops_shared_key="real-key-4")
    with pytest.raises(ConfigurationError, match="not a valid database URL"):
        validate_production_config(s)


def test_validate_production_config_rejects_missing_groq_key():
    s = Settings(
        app_env="production",
        database_url="postgresql+psycopg://real:S3cure@prod-db:5432/interview",
        llm_provider="groq", groq_api_key="",
        deepgram_api_key="real",
        agent_shared_key="real-agent-key-1",
        recruiter_shared_key="real-recruiter-key-2",
        worker_shared_key="real-worker-key-3",
        ops_shared_key="real-ops-key-4",
    )
    with pytest.raises(ConfigurationError, match="GROQ_API_KEY"):
        validate_production_config(s)


def test_validate_production_config_skips_groq_check_for_other_llm_provider():
    s = Settings(
        app_env="production",
        database_url="postgresql+psycopg://real:S3cure@prod-db:5432/interview",
        llm_provider="ollama", groq_api_key="",
        deepgram_api_key="real",
        agent_shared_key="real-agent-key-1",
        recruiter_shared_key="real-recruiter-key-2",
        worker_shared_key="real-worker-key-3",
        ops_shared_key="real-ops-key-4",
    )
    validate_production_config(s)


@pytest.mark.parametrize("stt, tts", [("deepgram", "other"), ("other", "deepgram")])
def test_validate_production_config_rejects_missing_deepgram_key_if_either_provider_uses_it(
        stt, tts):
    s = Settings(
        app_env="production",
        database_url="postgresql+psycopg://real:S3cure@prod-db:5432/interview",
        groq_api_key="gsk_real",
        stt_provider=stt, tts_provider=tts, deepgram_api_key="",
        agent_shared_key="real-agent-key-1",
        recruiter_shared_key="real-recruiter-key-2",
        worker_shared_key="real-worker-key-3",
        ops_shared_key="real-ops-key-4",
    )
    with pytest.raises(ConfigurationError, match="DEEPGRAM_API_KEY"):
        validate_production_config(s)


def test_validate_production_config_skips_deepgram_check_when_neither_provider_uses_it():
    s = Settings(
        app_env="production",
        database_url="postgresql+psycopg://real:S3cure@prod-db:5432/interview",
        groq_api_key="gsk_real",
        stt_provider="other", tts_provider="other", deepgram_api_key="",
        agent_shared_key="real-agent-key-1",
        recruiter_shared_key="real-recruiter-key-2",
        worker_shared_key="real-worker-key-3",
        ops_shared_key="real-ops-key-4",
    )
    validate_production_config(s)


def test_validate_production_config_rejects_incomplete_twilio_config():
    s = Settings(
        app_env="production",
        database_url="postgresql+psycopg://real:S3cure@prod-db:5432/interview",
        groq_api_key="gsk_real", deepgram_api_key="real",
        transport_provider="twilio",
        twilio_account_sid="", twilio_auth_token="", twilio_phone_number="", public_host="",
        agent_shared_key="real-agent-key-1",
        recruiter_shared_key="real-recruiter-key-2",
        worker_shared_key="real-worker-key-3",
        ops_shared_key="real-ops-key-4",
    )
    with pytest.raises(ConfigurationError, match="TRANSPORT_PROVIDER=twilio"):
        validate_production_config(s)


def test_validate_production_config_skips_twilio_checks_for_webrtc_transport():
    s = Settings(
        app_env="production",
        database_url="postgresql+psycopg://real:S3cure@prod-db:5432/interview",
        groq_api_key="gsk_real", deepgram_api_key="real",
        transport_provider="webrtc",
        twilio_account_sid="", twilio_auth_token="", twilio_phone_number="", public_host="",
        agent_shared_key="real-agent-key-1",
        recruiter_shared_key="real-recruiter-key-2",
        worker_shared_key="real-worker-key-3",
        ops_shared_key="real-ops-key-4",
    )
    validate_production_config(s)


def test_validate_production_config_reports_every_simultaneous_failure():
    s = Settings(
        app_env="production",
        database_url="postgresql+psycopg://interview:interview@postgres:5432/interview",
        llm_provider="groq", groq_api_key="",
        stt_provider="deepgram", deepgram_api_key="",
        transport_provider="twilio",
        twilio_account_sid="", twilio_auth_token="", twilio_phone_number="", public_host="",
        agent_shared_key="real-agent-key-1",
        recruiter_shared_key="real-recruiter-key-2",
        worker_shared_key="real-worker-key-3",
        ops_shared_key="real-ops-key-4",
    )
    with pytest.raises(ConfigurationError) as excinfo:
        validate_production_config(s)
    message = str(excinfo.value)
    assert "DATABASE_URL" in message
    assert "GROQ_API_KEY" in message
    assert "DEEPGRAM_API_KEY" in message
    assert "TRANSPORT_PROVIDER=twilio" in message


def test_validate_production_config_accepts_fully_configured_production():
    s = Settings(
        app_env="production",
        database_url="postgresql+psycopg://real:S3cure@prod-db:5432/interview",
        groq_api_key="gsk_real", deepgram_api_key="real",
        agent_shared_key="real-agent-key-1",
        recruiter_shared_key="real-recruiter-key-2",
        worker_shared_key="real-worker-key-3",
        ops_shared_key="real-ops-key-4",
    )
    validate_production_config(s)


# ---- PR-012: end-to-end through the real FastAPI lifespan (no reload) ----

def test_app_startup_fails_closed_on_placeholder_secrets(monkeypatch):
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "database_url",
                        "postgresql+psycopg://interview:interview@postgres:5432/interview")
    monkeypatch.setattr(settings, "groq_api_key", "gsk_real")
    monkeypatch.setattr(settings, "deepgram_api_key", "real")

    with pytest.raises(ConfigurationError):
        with TestClient(main_module.app):
            pass


def test_app_startup_succeeds_with_valid_production_config(monkeypatch):
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "database_url",
                        "postgresql+psycopg://real:S3cure@prod-db:5432/interview")
    monkeypatch.setattr(settings, "groq_api_key", "gsk_real")
    monkeypatch.setattr(settings, "deepgram_api_key", "real")
    monkeypatch.setattr(settings, "agent_shared_key", "real-agent-key-1")
    monkeypatch.setattr(settings, "recruiter_shared_key", "real-recruiter-key-2")
    monkeypatch.setattr(settings, "worker_shared_key", "real-worker-key-3")
    monkeypatch.setattr(settings, "ops_shared_key", "real-ops-key-4")

    with TestClient(main_module.app) as client:
        assert client.post("/scenarios/interrupted", json={}).status_code == 404


# ---- docker-compose.prod.yml: no hardcoded dev credentials, APP_ENV is production ----

_COMPOSE_PROD_PATH = pathlib.Path(__file__).resolve().parents[2] / "docker-compose.prod.yml"


def test_compose_prod_does_not_hardcode_dev_db_credentials():
    text = _COMPOSE_PROD_PATH.read_text()
    assert "postgresql+psycopg://interview:interview@" not in text
    assert "POSTGRES_PASSWORD: interview" not in text
    assert "POSTGRES_USER: interview" not in text


def test_compose_prod_sources_db_credentials_from_env():
    text = _COMPOSE_PROD_PATH.read_text()
    assert "DATABASE_URL: postgresql+psycopg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@postgres:5432/${POSTGRES_DB}" in text
    assert "POSTGRES_USER: ${POSTGRES_USER}" in text
    assert "POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}" in text
    assert "POSTGRES_DB: ${POSTGRES_DB}" in text


def test_compose_prod_sets_app_env_production():
    text = _COMPOSE_PROD_PATH.read_text()
    assert "APP_ENV: production" in text

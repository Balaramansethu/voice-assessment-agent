"""Central configuration, loaded from environment / .env."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"
    app_host: str = "0.0.0.0"
    app_port: int = 8000

    database_url: str = "postgresql+psycopg://interview:interview@postgres:5432/interview"

    # transport: webrtc (default, browser) | twilio (optional PSTN)
    transport_provider: str = "webrtc"

    # inference — Groq LLM, Deepgram STT/TTS by default
    stt_provider: str = "deepgram"  # deepgram
    llm_provider: str = "groq"      # groq | ollama
    tts_provider: str = "deepgram"  # deepgram
    groq_api_key: str = ""
    # Conversational voice model. This Groq tier exposes NO non-reasoning chat
    # model (the llama-3.3 family 404s here), so we use gpt-oss-120b and hide its
    # chain-of-thought via extra_body reasoning_format="hidden" (set on the voice
    # GroqLLMService in pipeline.py). SpeakableTextFilter is the safety net that
    # strips any residual punctuation-only fragments before they reach TTS.
    groq_llm_model: str = "qwen/qwen3.8-27b"
    # Silent grader model — separate from the voice model so grading can use a
    # strong reasoning model (never spoken) without affecting turn latency.
    groq_grader_model: str = "openai/gpt-oss-120b"

    # Deepgram — streaming STT (Nova) + TTS (Aura)
    deepgram_api_key: str = ""
    deepgram_stt_model: str = "nova-3"          # Nova-3 general (better accuracy, keyterm boosting)
    deepgram_tts_voice: str = "aura-2-thalia-en"  # natural Aura-2 English voice

    # End-of-turn detection (agent-side; read via os.getenv in app/agent/pipeline.py).
    #   smart_turn — Smart Turn v3 ONNX prosody model is the PRIMARY end-of-turn decider:
    #                it reads prosody and says COMPLETE when the caller has genuinely
    #                finished, holding the turn open through mid-answer pauses. The
    #                hard-silence backstop below is only a safety net for a stuck turn.
    #   vad        — fall back to crude fixed-silence VAD endpointing (offline/low-CPU).
    turn_detection: str = "smart_turn"          # smart_turn | vad
    smart_turn_cpu_count: int = 2               # ONNX inference threads for Smart Turn
    # Hard-silence backstop inside the Smart Turn analyzer. Smart Turn is the primary
    # decider; this only RESCUES a turn the model never resolves (never says COMPLETE).
    # Set generously (2.0s) so a caller mid-thought is not force-closed and fragmented:
    # a tighter value (e.g. 1.0s) fires while Smart Turn still judges the turn INCOMPLETE,
    # cutting answers off. VAD stop_secs (0.2) is only the onset trigger, not this.
    smart_turn_stop_secs: float = 2.0
    # Barge-in gate. While the bot is speaking, an interruption fires only once the
    # caller has spoken at least this many transcribed words — noise/breath/echo and
    # one-word blips no longer cancel the bot's reply, but a real sentence still cuts
    # in within ~1s. When the bot is silent a single word starts the turn as usual.
    interruption_min_words: int = 3

    # offline fallback (optional)
    ollama_base_url: str = "http://ollama:11434/v1"
    ollama_model: str = "llama3.1:8b"
    whisper_model: str = "small"

    # optional PSTN
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_phone_number: str = ""
    public_host: str = ""           # public domain (for Twilio signature validation)

    # PR-105 — scope shared-secret keys (rotate independently per scope in production)
    agent_shared_key: str = "dev-agent-key-change-me"
    recruiter_shared_key: str = "dev-recruiter-key-change-me"
    worker_shared_key: str = "dev-worker-key-change-me"
    ops_shared_key: str = "dev-ops-key-change-me"

    # PR-021 — public voice-entry hardening: token TTL, concurrency ceiling,
    # per-number rate limit, max call duration. Postgres/application-enforced,
    # no new infra. max_call_duration_seconds is also read directly via
    # os.getenv() in app/agent/pipeline.py (the agent process doesn't import
    # app.config — see Dockerfile.agent) — same pattern as TURN_DETECTION /
    # SMART_TURN_STOP_SECS / INTERRUPTION_MIN_WORDS elsewhere in this file.
    voice_session_token_ttl_seconds: int = 30
    max_concurrent_calls: int = 5
    max_calls_per_number_per_minute: int = 3
    max_call_duration_seconds: int = 900

    # interview config
    interview_expiry_hours: int = 72

    # Observability — LangSmith tracing (tokens, latency, errors, step traces).
    # Tracing activates only when langsmith_tracing is true AND a key is set.
    langsmith_tracing: bool = True
    langsmith_api_key: str = ""
    langsmith_project: str = "observability"
    langsmith_endpoint: str = "https://api.smith.langchain.com"
    # Opt-in, production-only: capture full trace payloads (transcripts, candidate
    # answers, rubric text) in LangSmith. Defaults False — in production, tracing
    # still records step names/latency/status, but PII/content-shaped fields are
    # redacted (see app/observability/tracing.py) unless this is explicitly set true.
    # Outside production this flag has no effect — full capture is the existing,
    # unchanged dev-debugging behavior. WARNING: true in production sends unredacted
    # candidate content to a third party (LangSmith).
    langsmith_capture_content: bool = False

    # RAG / retrieval
    embedding_provider: str = "fastembed"          # fastembed | (future: ollama, hosted)
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_dim: int = 384
    rag_top_k: int = 8                              # candidates per retriever before fusion
    rag_final_k: int = 4                            # chunks handed to the LLM
    rag_min_score: float = 0.45                     # below this → insufficient context → escalate
    rag_rerank: bool = False                        # optional cross-encoder rerank (Phase 2+)


settings = Settings()


class ConfigurationError(RuntimeError):
    """Raised at server startup when production config carries a placeholder/dev
    default. Never raised outside app_env == 'production'."""


_DEV_DB_USER = "interview"
_DEV_DB_PASSWORD = "interview"


def validate_production_config(s: Settings) -> None:
    """Fail closed: refuse to start in production with placeholder secrets or the
    committed dev-default database credentials. No-op outside production.

    Called from app/main.py's lifespan, BEFORE init_db() — this is a server-startup
    gate that reads settings.app_env at call time, not at module-import time, so
    scripts/tests that merely import `settings` are unaffected even if APP_ENV
    happens to be 'production' in their environment.
    """
    if s.app_env.strip().lower() != "production":
        return

    errors: list[str] = []

    _dev_scope_defaults = {
        "AGENT_SHARED_KEY": ("agent_shared_key", "dev-agent-key-change-me"),
        "RECRUITER_SHARED_KEY": ("recruiter_shared_key", "dev-recruiter-key-change-me"),
        "WORKER_SHARED_KEY": ("worker_shared_key", "dev-worker-key-change-me"),
        "OPS_SHARED_KEY": ("ops_shared_key", "dev-ops-key-change-me"),
    }
    seen_scope_values: set[str] = set()
    for env_name, (attr, dev_default) in _dev_scope_defaults.items():
        value = getattr(s, attr)
        if not value.strip():
            errors.append(f"{env_name} must be set in production.")
        elif value == dev_default:
            errors.append(f"{env_name} still uses its committed dev-default value.")
        elif value in seen_scope_values:
            errors.append(f"{env_name} must not reuse another scope's secret value.")
        seen_scope_values.add(value)

    try:
        from sqlalchemy.engine import make_url
        url = make_url(s.database_url)
        if not (url.username or "").strip() or not (url.password or "").strip():
            errors.append("DATABASE_URL is missing a username or password.")
        elif url.username == _DEV_DB_USER and url.password == _DEV_DB_PASSWORD:
            errors.append(
                "DATABASE_URL still uses the committed dev-default credentials "
                "(username/password 'interview') — set a production DATABASE_URL "
                "with real, rotated credentials, regardless of host."
            )
    except Exception:
        errors.append("DATABASE_URL is not a valid database URL.")

    if s.llm_provider == "groq" and not s.groq_api_key:
        errors.append("LLM_PROVIDER=groq requires GROQ_API_KEY to be set.")
    if (s.stt_provider == "deepgram" or s.tts_provider == "deepgram") and not s.deepgram_api_key:
        errors.append(
            "STT_PROVIDER or TTS_PROVIDER is deepgram, which requires "
            "DEEPGRAM_API_KEY to be set."
        )
    if s.transport_provider == "twilio":
        missing = [
            name for name, value in (
                ("TWILIO_ACCOUNT_SID", s.twilio_account_sid),
                ("TWILIO_AUTH_TOKEN", s.twilio_auth_token),
                ("TWILIO_PHONE_NUMBER", s.twilio_phone_number),
                ("PUBLIC_HOST", s.public_host),
            )
            if not value
        ]
        if missing:
            errors.append(
                "TRANSPORT_PROVIDER=twilio requires " + ", ".join(missing) + " to be set."
            )

    if errors:
        raise ConfigurationError(
            "Refusing to start in production with unsafe configuration:\n- "
            + "\n- ".join(errors)
        )

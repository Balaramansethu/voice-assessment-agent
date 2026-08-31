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

    # inference — Groq free tier by default
    stt_provider: str = "groq"      # groq | whisper
    llm_provider: str = "groq"      # groq | ollama
    groq_api_key: str = ""
    groq_llm_model: str = "openai/gpt-oss-120b"
    groq_stt_model: str = "whisper-large-v3"

    # TTS — local, no key
    tts_provider: str = "kokoro"    # kokoro | piper
    tts_voice: str = "af_heart"

    # offline fallback (optional)
    ollama_base_url: str = "http://ollama:11434/v1"
    ollama_model: str = "llama3.1:8b"
    whisper_model: str = "small"

    # optional PSTN
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_phone_number: str = ""
    public_host: str = ""           # public domain (for Twilio signature validation)

    # interview config
    interview_expiry_hours: int = 72

    # Observability — LangSmith tracing (tokens, latency, errors, step traces).
    # Tracing activates only when langsmith_tracing is true AND a key is set.
    langsmith_tracing: bool = True
    langsmith_api_key: str = ""
    langsmith_project: str = "observability"
    langsmith_endpoint: str = "https://api.smith.langchain.com"

    # RAG / retrieval
    embedding_provider: str = "fastembed"          # fastembed | (future: ollama, hosted)
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_dim: int = 384
    rag_top_k: int = 8                              # candidates per retriever before fusion
    rag_final_k: int = 4                            # chunks handed to the LLM
    rag_min_score: float = 0.45                     # below this → insufficient context → escalate
    rag_rerank: bool = False                        # optional cross-encoder rerank (Phase 2+)


settings = Settings()

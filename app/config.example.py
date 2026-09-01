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
    #   smart_turn — Smart Turn v3 ONNX prosody model decides when the caller is done
    #                (fast when confident; VAD start still drives onset/interruptions,
    #                and the analyzer's own 3s silence backstop guarantees a turn ends).
    #   vad        — fall back to crude fixed-silence VAD endpointing (offline/low-CPU).
    turn_detection: str = "smart_turn"          # smart_turn | vad
    smart_turn_cpu_count: int = 1               # ONNX inference threads for Smart Turn

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

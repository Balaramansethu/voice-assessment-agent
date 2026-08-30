"""Health + config-visibility endpoint (no secrets)."""
from __future__ import annotations

from fastapi import APIRouter

from app.config import settings

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "transport": settings.transport_provider,
        "stt": settings.stt_provider,
        "llm": settings.llm_provider,
        "tts": settings.tts_provider,
        "groq_key_present": bool(settings.groq_api_key),
    }

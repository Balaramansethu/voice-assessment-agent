"""API request/response models."""
from __future__ import annotations

from pydantic import BaseModel

from app.domain.states import Intent


class ScenarioRequest(BaseModel):
    name: str = "Rahul"
    phone: str = "+919000000001"
    email: str = "rahul@example.com"
    role: str = "Backend Engineer"


class IntentRequest(BaseModel):
    interview_id: int
    intent: Intent
    call_id: int | None = None


class AnswerRequest(BaseModel):
    transcript: str
    call_id: int | None = None


class ResolveRequest(BaseModel):
    phone: str | None = None
    identifier: str | None = None


class IdentifyRequest(BaseModel):
    name: str
    call_id: int | None = None


class InboundCallRequest(BaseModel):
    """Mirrors the fields we care about from a Twilio inbound webhook, but works
    for the WebRTC demo too."""
    provider_call_id: str
    from_number: str
    to_number: str | None = None
    transport: str = "webrtc"

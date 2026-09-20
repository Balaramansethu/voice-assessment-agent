"""API request/response models."""
from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, StringConstraints

from app.domain.states import CallStatus, Intent


def bounded_str(max_length: int, *, min_length: int = 1, strip: bool = True) -> type:
    """Annotated str field type: non-blank, capped length, strict (no int/bool coercion).

    Shared by assessment/rag request fields (role, name, ids, queries) so every bounded
    field uses one strict/min/max convention instead of a duplicated StringConstraints
    call per field. `strip=False` is for fields where whitespace-only is meaningful data
    (e.g. an STT-silence transcript) rather than formatting noise to normalize away.
    """
    return Annotated[
        str, StringConstraints(strict=True, strip_whitespace=strip,
                               min_length=min_length, max_length=max_length)
    ]


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

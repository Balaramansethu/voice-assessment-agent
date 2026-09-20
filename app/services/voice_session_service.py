"""One-use Twilio voice-session tokens (PR-016..019): mint at the signature-
guarded webhook, atomically consume once at WebSocket establishment. Postgres
is the only source of truth — no Redis, no in-memory state. The token proves
nothing durable; it's a ~30s, single-use claim that "this WSS connection is
the one Twilio's signed webhook told us to expect for this CallSid."
"""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import VoiceSessionToken


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _digest(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode()).hexdigest()


def mint_token(
    session: Session,
    *,
    provider_call_id: str,
    from_number: str | None,
    to_number: str | None,
    purpose: str = "twilio_stream",
) -> str:
    """Create a one-use token row and return the RAW token — the only moment
    it exists in plaintext. Only its sha256 digest is persisted. Caller
    commits.

    Twilio retries a slow webhook response with the SAME CallSid — invalidate
    any still-live (unconsumed) token for this CallSid first, since the
    partial unique index on voice_session_token allows only one live token
    per CallSid. We can't hand back the OLD raw token (only its digest
    survives a mint), so a retry gets a genuinely fresh one and the old one
    is marked dead instead."""
    session.execute(
        update(VoiceSessionToken)
        .where(VoiceSessionToken.provider_call_id == provider_call_id,
               VoiceSessionToken.consumed_at.is_(None))
        .values(consumed_at=_now())
    )
    raw = secrets.token_urlsafe(32)
    now = _now()
    session.add(VoiceSessionToken(
        token_digest=_digest(raw),
        provider_call_id=provider_call_id,
        from_number=from_number,
        to_number=to_number,
        purpose=purpose,
        issued_at=now,
        expires_at=now + timedelta(seconds=settings.voice_session_token_ttl_seconds),
    ))
    session.flush()
    return raw


@dataclass
class TokenClaims:
    token_id: int
    provider_call_id: str
    from_number: str | None
    to_number: str | None


def consume_token(session: Session, raw_token: str) -> TokenClaims | None:
    """Atomically claim a token: a single UPDATE ... WHERE consumed_at IS NULL
    AND expires_at > now() ... RETURNING. Race-safe against a concurrent
    double-consume of the SAME raw token — no SELECT-then-UPDATE gap. Returns
    None (never raises) for missing/expired/already-consumed/garbage tokens.
    Does NOT commit — caller commits."""
    digest = _digest(raw_token)
    stmt = (
        update(VoiceSessionToken)
        .where(
            VoiceSessionToken.token_digest == digest,
            VoiceSessionToken.consumed_at.is_(None),
            VoiceSessionToken.expires_at > _now(),
        )
        .values(consumed_at=_now())
        .returning(
            VoiceSessionToken.id, VoiceSessionToken.provider_call_id,
            VoiceSessionToken.from_number, VoiceSessionToken.to_number,
        )
    )
    row = session.execute(stmt).first()
    if row is None:
        return None
    return TokenClaims(token_id=row[0], provider_call_id=row[1],
                       from_number=row[2], to_number=row[3])


def bind_call(session: Session, token_id: int, call_id: int) -> None:
    session.execute(
        update(VoiceSessionToken).where(VoiceSessionToken.id == token_id)
        .values(call_id=call_id)
    )


def rate_limited(session: Session, *, from_number: str) -> bool:
    """PR-021: True if `from_number` has had >= max_calls_per_number_per_minute
    tokens ISSUED (not just consumed) in the last 60s — counts every signed
    webhook POST including Twilio's own retries, bounding blast radius
    regardless of whether a prior token was ever redeemed."""
    window_start = _now() - timedelta(seconds=60)
    count = session.scalar(
        select(func.count(VoiceSessionToken.id)).where(
            VoiceSessionToken.from_number == from_number,
            VoiceSessionToken.issued_at >= window_start,
        )
    )
    return (count or 0) >= settings.max_calls_per_number_per_minute

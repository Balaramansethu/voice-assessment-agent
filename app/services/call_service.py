"""Call lifecycle. Call state is independent of interview state."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import Call, VoiceSessionToken
from app.domain.states import CallDirection, CallStatus


def ingest_call(
    session: Session,
    *,
    provider_call_id: str,
    direction: CallDirection,
    status: CallStatus,
    transport: str = "webrtc",
    from_number: str | None = None,
    to_number: str | None = None,
    candidate_id: int | None = None,
    interview_id: int | None = None,
) -> Call:
    """Idempotent create-or-fetch keyed on provider_call_id. Retried webhooks and
    double taps collapse to a single row (INSERT ... ON CONFLICT DO NOTHING)."""
    stmt = (
        pg_insert(Call)
        .values(
            provider_call_id=provider_call_id,
            direction=direction.value,
            status=status.value,
            transport=transport,
            from_number=from_number,
            to_number=to_number,
            candidate_id=candidate_id,
            interview_id=interview_id,
        )
        .on_conflict_do_nothing(index_elements=["provider_call_id"])
    )
    session.execute(stmt)
    session.flush()
    return session.scalar(select(Call).where(Call.provider_call_id == provider_call_id))


def set_status(session: Session, call: Call, status: CallStatus) -> Call:
    call.status = status.value
    session.flush()
    return call


def has_active_call(session: Session, interview_id: int) -> bool:
    return session.scalar(
        select(Call.id).where(
            Call.interview_id == interview_id,
            Call.status == CallStatus.ACTIVE.value,
        )
    ) is not None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def at_concurrency_ceiling(session: Session) -> bool:
    """PR-021 concurrency ceiling, Postgres-authoritative (no Redis).

    Counts both ACTIVE Call rows (connected) and live, unconsumed VoiceSessionToken
    rows (reserved-but-not-yet-connected slots). For Twilio, the Call row doesn't
    exist until /telephony/session/consume runs, well after the webhook already
    minted a token — without counting tokens, a burst of concurrent webhook requests
    would all see "0 active calls" and mint before any showed up as Call rows, bypassing
    the ceiling entirely. A live token (unconsumed, unexpired) represents a reserved
    slot; once consumed, it becomes an ACTIVE Call and the token stops counting
    (no double-count). The advisory lock in _handle_twilio_voice serializes the
    check-and-mint sequence, so each request in a burst sees the accurately-updated
    count left by its predecessor.

    Stopgap for crash recovery (full lease/reconciliation is P2/PR-209): the
    ONLY path out of ACTIVE today is on_client_disconnected firing in a live
    agent process. If that process crashes/OOMs mid-call (torch/kokoro-heavy,
    per Dockerfile.agent), the Call row stays ACTIVE forever and this ceiling
    would count it forever — a handful of crashes permanently locks out every
    future caller. We treat any ACTIVE call older than 2x the configured max
    call duration (generous margin — no real call is legitimately still
    ACTIVE that long) as an orphan and force-end it FAILED right here, before
    counting. Uses Call.created_at, not started_at: started_at is never
    written anywhere in this codebase today and would make this check
    silently inert. This is a bounded, on-demand self-heal (fires at mint
    time, exactly when a retrying caller needs it), not a background reaper
    — it bounds "N crashes = permanent lockout" to "self-heals within
    roughly 2x max call duration," not the final answer P2/PR-209 owns."""
    stale_cutoff = _now() - timedelta(seconds=settings.max_call_duration_seconds * 2)
    stale = session.scalars(
        select(Call).where(
            Call.status == CallStatus.ACTIVE.value,
            Call.created_at < stale_cutoff,
        )
    ).all()
    for call in stale:
        end_call(session, call, CallStatus.FAILED)
    session.flush()

    active_count = session.scalar(
        select(func.count(Call.id)).where(Call.status == CallStatus.ACTIVE.value)
    )
    reserved_count = session.scalar(
        select(func.count(VoiceSessionToken.id)).where(
            VoiceSessionToken.consumed_at.is_(None),
            VoiceSessionToken.expires_at > _now(),
        )
    )
    return (active_count or 0) + (reserved_count or 0) >= settings.max_concurrent_calls


def end_call(session: Session, call: Call, status: CallStatus) -> Call:
    """Stamp a terminal call status + ended_at, atomically and idempotently.

    Uses a single UPDATE ... WHERE ended_at IS NULL ... RETURNING rather than
    a read-then-write Python check — a read-then-write check does NOT give
    "first writer wins" under genuine concurrency (the last committer can
    silently overwrite the first's status/timestamp). Whichever caller's
    UPDATE commits first is the one true terminal state; a second concurrent
    caller's WHERE clause matches zero rows and it gets back the
    already-ended row, unchanged."""
    now = _now()
    stmt = (
        update(Call)
        .where(Call.id == call.id, Call.ended_at.is_(None))
        .values(status=status.value, ended_at=now)
        .returning(Call.status, Call.ended_at)
    )
    row = session.execute(stmt).first()
    if row is not None:
        call.status, call.ended_at = row[0], row[1]
    else:
        session.refresh(call)  # another writer already ended it — pick up its values
    return call

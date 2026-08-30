"""Call lifecycle. Call state is independent of interview state."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db.models import Call
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

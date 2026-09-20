"""Interview + question/answer reads and derived state. No transitions here —
those go through the orchestrator so they happen under a row lock."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Call, Interview, InterviewAnswer, InterviewEvent, InterviewQuestion
from app.domain.states import InterviewStatus


def get_active_interview(session: Session, candidate_id: int) -> Interview | None:
    """The interview a callback should attach to: newest non-terminal one."""
    terminal = {
        InterviewStatus.COMPLETED.value,
        InterviewStatus.CANCELLED.value,
        InterviewStatus.EXPIRED.value,
    }
    return session.scalar(
        select(Interview)
        .where(Interview.candidate_id == candidate_id, Interview.status.notin_(terminal))
        .order_by(Interview.created_at.desc())
    )


def list_active_interviews(session: Session, candidate_id: int) -> list[Interview]:
    terminal = {
        InterviewStatus.COMPLETED.value,
        InterviewStatus.CANCELLED.value,
        InterviewStatus.EXPIRED.value,
    }
    return list(
        session.scalars(
            select(Interview).where(
                Interview.candidate_id == candidate_id,
                Interview.status.notin_(terminal),
            )
        )
    )


def effective_status(interview: Interview) -> InterviewStatus:
    """Derive EXPIRED from expires_at rather than trusting a stale column."""
    status = InterviewStatus(interview.status)
    if status in (InterviewStatus.NOT_STARTED, InterviewStatus.INTERRUPTED,
                  InterviewStatus.RESCHEDULED) and interview.expires_at:
        if interview.expires_at < datetime.now(timezone.utc):
            return InterviewStatus.EXPIRED
    return status


def next_unanswered_position(session: Session, interview: Interview) -> int | None:
    """Resume position, derived from answers — never a trusted in-memory pointer.
    Returns the position of the first question with no answer row, or None if done."""
    questions = session.scalars(
        select(InterviewQuestion)
        .where(InterviewQuestion.interview_id == interview.id)
        .order_by(InterviewQuestion.position)
    ).all()
    answered_qids = set(
        session.scalars(
            select(InterviewAnswer.question_id).where(
                InterviewAnswer.interview_id == interview.id
            )
        ).all()
    )
    for q in questions:
        if q.id not in answered_qids:
            return q.position
    return None


def question_text_at(session: Session, interview: Interview, position: int) -> str | None:
    q = session.scalar(
        select(InterviewQuestion).where(
            InterviewQuestion.interview_id == interview.id,
            InterviewQuestion.position == position,
        )
    )
    return q.prompt_text if q else None


def record_event(
    session: Session,
    *,
    event_type: str,
    interview_id: int | None = None,
    call_id: int | None = None,
    payload: dict | None = None,
) -> None:
    session.add(
        InterviewEvent(
            interview_id=interview_id,
            call_id=call_id,
            event_type=event_type,
            payload=payload,
        )
    )


def invitation_attempts_rejected(session: Session, call_id: int) -> int:
    return session.scalar(
        select(func.count(InterviewEvent.id)).where(
            InterviewEvent.call_id == call_id,
            InterviewEvent.event_type == "INVITATION_CODE_REJECTED")
    ) or 0


def call_owns_interview(session: Session, call_id: int, interview_id: int) -> bool:
    return session.scalar(
        select(Call.id).where(Call.id == call_id, Call.interview_id == interview_id)
    ) is not None

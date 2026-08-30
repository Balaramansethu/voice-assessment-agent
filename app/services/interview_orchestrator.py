"""The Interview Orchestrator — the only component that mutates interview state.

The LLM proposes an intent; this decides whether the action is legal and, if so,
performs the transition inside a transaction that row-locks the interview
(SELECT ... FOR UPDATE) so two concurrent callbacks can't both start/resume it.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from sqlalchemy import select as _select

from app.db.models import Interview, InterviewAnswer, InterviewQuestion
from app.domain.states import (
    Action,
    Intent,
    InterviewStatus,
    IllegalTransition,
    assert_transition,
    resolve_action,
)
from app.observability.tracing import traceable
from app.services import interview_service as iv
from app.services.call_service import has_active_call


@dataclass
class OrchestrationResult:
    action: Action
    allowed: bool
    interview_id: int
    status: InterviewStatus
    current_question: int | None
    message: str
    current_question_text: str | None = None


def _lock_interview(session: Session, interview_id: int) -> Interview:
    interview = session.scalar(
        select(Interview).where(Interview.id == interview_id).with_for_update()
    )
    if interview is None:
        raise ValueError(f"interview {interview_id} not found")
    return interview


def _now() -> datetime:
    return datetime.now(timezone.utc)


@traceable(run_type="chain", name="orchestrator.decide_and_execute")
def decide_and_execute(
    session: Session,
    *,
    interview_id: int,
    intent: Intent,
    call_id: int | None = None,
) -> OrchestrationResult:
    """Row-lock the interview, resolve the action from (status, intent), validate
    the transition, persist, and log an event. All-or-nothing within the caller's
    transaction."""
    interview = _lock_interview(session, interview_id)
    status = iv.effective_status(interview)
    action = resolve_action(status, intent)

    if action == Action.REJECT:
        iv.record_event(
            session, event_type="ACTION_REJECTED", interview_id=interview.id,
            call_id=call_id, payload={"intent": intent.value, "status": status.value},
        )
        return OrchestrationResult(
            action=action, allowed=False, interview_id=interview.id, status=status,
            current_question=interview.current_question,
            message=_reject_message(status),
        )

    if action == Action.ESCALATE:
        iv.record_event(
            session, event_type="ESCALATION_REQUESTED", interview_id=interview.id,
            call_id=call_id, payload={"intent": intent.value, "status": status.value},
        )
        return OrchestrationResult(
            action=action, allowed=True, interview_id=interview.id, status=status,
            current_question=interview.current_question,
            message="Connecting you with the recruiting team.",
        )

    try:
        if action == Action.START:
            return _start(session, interview, call_id)
        if action == Action.RESUME:
            return _resume(session, interview, call_id)
        if action == Action.RESCHEDULE:
            return _reschedule(session, interview, call_id)
    except IllegalTransition as exc:
        iv.record_event(
            session, event_type="ACTION_REJECTED", interview_id=interview.id,
            call_id=call_id, payload={"reason": str(exc)},
        )
        return OrchestrationResult(
            action=Action.REJECT, allowed=False, interview_id=interview.id,
            status=status, current_question=interview.current_question,
            message=_reject_message(status),
        )

    raise RuntimeError(f"unhandled action {action}")


def _start(session: Session, interview: Interview, call_id: int | None) -> OrchestrationResult:
    if has_active_call(session, interview.id):
        return OrchestrationResult(
            action=Action.REJECT, allowed=False, interview_id=interview.id,
            status=InterviewStatus(interview.status),
            current_question=interview.current_question,
            message="This interview is already in progress on another call.",
        )
    assert_transition(InterviewStatus(interview.status), InterviewStatus.IN_PROGRESS)
    interview.status = InterviewStatus.IN_PROGRESS.value
    interview.started_at = _now()
    first = iv.next_unanswered_position(session, interview) or 1
    interview.current_question = first
    session.flush()
    iv.record_event(session, event_type="INTERVIEW_STARTED", interview_id=interview.id,
                    call_id=call_id, payload={"question": first})
    return OrchestrationResult(
        action=Action.START, allowed=True, interview_id=interview.id,
        status=InterviewStatus.IN_PROGRESS, current_question=first,
        message="Great, let's begin the interview.",
        current_question_text=iv.question_text_at(session, interview, first),
    )


def _resume(session: Session, interview: Interview, call_id: int | None) -> OrchestrationResult:
    # Only INTERRUPTED needs a real transition into IN_PROGRESS; an already
    # IN_PROGRESS interview is continued in place (no transition to validate).
    if interview.status == InterviewStatus.INTERRUPTED.value:
        assert_transition(InterviewStatus.INTERRUPTED, InterviewStatus.IN_PROGRESS)
    resume_at = iv.next_unanswered_position(session, interview)
    if resume_at is None:  # all questions answered -> should be completed, not resumed
        interview.status = InterviewStatus.COMPLETED.value
        interview.completed_at = _now()
        session.flush()
        iv.record_event(session, event_type="INTERVIEW_COMPLETED",
                        interview_id=interview.id, call_id=call_id)
        return OrchestrationResult(
            action=Action.REJECT, allowed=False, interview_id=interview.id,
            status=InterviewStatus.COMPLETED, current_question=None,
            message="Your interview has already been completed.",
        )
    interview.status = InterviewStatus.IN_PROGRESS.value
    interview.current_question = resume_at
    session.flush()
    iv.record_event(session, event_type="INTERVIEW_RESUMED", interview_id=interview.id,
                    call_id=call_id, payload={"question": resume_at})
    return OrchestrationResult(
        action=Action.RESUME, allowed=True, interview_id=interview.id,
        status=InterviewStatus.IN_PROGRESS, current_question=resume_at,
        message=f"Welcome back. Let's continue from question {resume_at}.",
        current_question_text=iv.question_text_at(session, interview, resume_at),
    )


def _reschedule(session: Session, interview: Interview, call_id: int | None) -> OrchestrationResult:
    assert_transition(InterviewStatus(interview.status), InterviewStatus.RESCHEDULED)
    interview.status = InterviewStatus.RESCHEDULED.value
    session.flush()
    iv.record_event(session, event_type="INTERVIEW_RESCHEDULED",
                    interview_id=interview.id, call_id=call_id)
    return OrchestrationResult(
        action=Action.RESCHEDULE, allowed=True, interview_id=interview.id,
        status=InterviewStatus.RESCHEDULED, current_question=interview.current_question,
        message="No problem, we'll reschedule your interview.",
    )


@dataclass
class AnswerResult:
    done: bool
    message: str
    position: int | None = None
    next_question_text: str | None = None


@traceable(run_type="chain", name="orchestrator.record_answer")
def record_answer(
    session: Session, *, interview_id: int, transcript: str, call_id: int | None = None,
) -> AnswerResult:
    """Record the candidate's answer to the current question, advance to the next
    unanswered one, and auto-complete when the last question is answered. Runs under
    the interview row lock so a stale pointer can't skip or repeat a question."""
    interview = _lock_interview(session, interview_id)
    if interview.status != InterviewStatus.IN_PROGRESS.value:
        return AnswerResult(done=False, message="No interview is currently in progress.")

    pos = iv.next_unanswered_position(session, interview)
    if pos is None:
        return _finish(session, interview, call_id)

    question = session.scalar(
        _select(InterviewQuestion).where(
            InterviewQuestion.interview_id == interview.id,
            InterviewQuestion.position == pos,
        )
    )
    session.add(InterviewAnswer(
        interview_id=interview.id, question_id=question.id,
        transcript=transcript, call_id=call_id,
    ))
    session.flush()
    iv.record_event(session, event_type="QUESTION_COMPLETED", interview_id=interview.id,
                    call_id=call_id, payload={"position": pos})

    next_pos = iv.next_unanswered_position(session, interview)
    if next_pos is None:
        return _finish(session, interview, call_id)

    interview.current_question = next_pos
    session.flush()
    text = iv.question_text_at(session, interview, next_pos)
    return AnswerResult(done=False, message=text, position=next_pos, next_question_text=text)


def _finish(session: Session, interview: Interview, call_id: int | None) -> AnswerResult:
    if interview.status == InterviewStatus.IN_PROGRESS.value:
        assert_transition(InterviewStatus.IN_PROGRESS, InterviewStatus.COMPLETED)
        interview.status = InterviewStatus.COMPLETED.value
        interview.completed_at = _now()
        session.flush()
        iv.record_event(session, event_type="INTERVIEW_COMPLETED",
                        interview_id=interview.id, call_id=call_id)
    return AnswerResult(
        done=True,
        message="That was the last question. Thank you — your interview is complete.",
    )


def complete_interview(session: Session, interview_id: int, call_id: int | None = None) -> None:
    interview = _lock_interview(session, interview_id)
    assert_transition(InterviewStatus(interview.status), InterviewStatus.COMPLETED)
    interview.status = InterviewStatus.COMPLETED.value
    interview.completed_at = _now()
    session.flush()
    iv.record_event(session, event_type="INTERVIEW_COMPLETED",
                    interview_id=interview.id, call_id=call_id)


def mark_interrupted(session: Session, interview_id: int, call_id: int | None = None) -> None:
    """Called on call-drop while IN_PROGRESS. Best-effort, idempotent."""
    interview = _lock_interview(session, interview_id)
    if interview.status != InterviewStatus.IN_PROGRESS.value:
        return
    interview.status = InterviewStatus.INTERRUPTED.value
    session.flush()
    iv.record_event(session, event_type="INTERVIEW_INTERRUPTED",
                    interview_id=interview.id, call_id=call_id,
                    payload={"question": interview.current_question})


def default_expiry() -> datetime:
    return _now() + timedelta(hours=settings.interview_expiry_hours)


def _reject_message(status: InterviewStatus) -> str:
    if status == InterviewStatus.COMPLETED:
        return ("I can see your interview has already been completed. "
                "I can connect you with the recruiting team if you need anything else.")
    if status == InterviewStatus.EXPIRED:
        return ("This interview window has expired. I can help you reschedule "
                "or connect you with the recruiting team.")
    return "I'm not able to do that right now. Let me connect you with the recruiting team."

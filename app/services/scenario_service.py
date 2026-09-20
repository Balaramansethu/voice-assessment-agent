"""Scenario simulator — seeds any candidate/interview/call state in one call so
the inbound voice flow is repeatable and deterministic for demos and tests."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db.models import (
    AnswerEvaluation,
    AssessmentSession,
    Call,
    Candidate,
    Interview,
    InterviewAnswer,
    InterviewEvent,
    InterviewQuestion,
)
from app.domain.states import CallDirection, CallStatus, InterviewStatus
from app.services.candidate_resolver import normalize_phone
from app.services import interview_orchestrator

DEFAULT_QUESTIONS = [
    "To start, could you tell me briefly about your background and your current role?",
    "What kind of backend systems have you worked with most recently?",
    "How would you design a rate limiter for an API?",
    "How do you approach database schema design?",
    "Tell me about a challenging production issue you debugged and how you resolved it.",
    "How do you make sure the code you ship is reliable and maintainable?",
    "Finally, why are you interested in this role, and do you have any questions for us?",
]


def _upsert_candidate(session: Session, name: str, phone: str, email: str) -> Candidate:
    phone = normalize_phone(phone)
    cand = session.scalar(select(Candidate).where(Candidate.phone == phone))
    if cand is None:
        cand = Candidate(name=name, phone=phone, email=email)
        session.add(cand)
        session.flush()
    return cand


def _fresh_interview(session: Session, cand: Candidate, role: str) -> Interview:
    # POC: one active interview per candidate — clear prior ones for a clean scenario.
    prior = session.scalars(select(Interview).where(Interview.candidate_id == cand.id)).all()
    for iv in prior:
        call_ids = list(
            session.scalars(select(Call.id).where(Call.interview_id == iv.id)).all()
        )
        # answer_evaluation references interview_answer — delete it first.
        session.execute(delete(AnswerEvaluation).where(AnswerEvaluation.interview_id == iv.id))
        session.execute(delete(InterviewAnswer).where(InterviewAnswer.interview_id == iv.id))
        session.execute(delete(InterviewQuestion).where(InterviewQuestion.interview_id == iv.id))
        # Events can reference the interview OR just one of its calls (e.g. an
        # unresolved inbound call). Delete both before removing the calls.
        event_filter = InterviewEvent.interview_id == iv.id
        if call_ids:
            event_filter = event_filter | InterviewEvent.call_id.in_(call_ids)
        session.execute(delete(InterviewEvent).where(event_filter))
        # PR-104's AssessmentSession.call_id/interview_id FKs (added after this function
        # was first written) also reference these rows — clear before deleting Call/Interview.
        session_filter = AssessmentSession.interview_id == iv.id
        if call_ids:
            session_filter = session_filter | AssessmentSession.call_id.in_(call_ids)
        session.execute(delete(AssessmentSession).where(session_filter))
        # A call may also carry candidate_id without interview_id — clear any call
        # for this candidate that would dangle.
        session.execute(delete(Call).where(Call.interview_id == iv.id))
        session.delete(iv)
    # Orphan inbound calls for this candidate (interview_id still NULL) + their events.
    orphan_call_ids = list(
        session.scalars(
            select(Call.id).where(Call.candidate_id == cand.id, Call.interview_id.is_(None))
        ).all()
    )
    if orphan_call_ids:
        session.execute(delete(InterviewEvent).where(InterviewEvent.call_id.in_(orphan_call_ids)))
        session.execute(delete(AssessmentSession).where(
            AssessmentSession.call_id.in_(orphan_call_ids)))
        session.execute(delete(Call).where(Call.id.in_(orphan_call_ids)))
    session.flush()

    interview = Interview(
        candidate_id=cand.id, role=role,
        status=InterviewStatus.NOT_STARTED.value,
        current_question=0, expires_at=interview_orchestrator.default_expiry(),
        invitation_code=interview_orchestrator.generate_invitation_code(),
    )
    session.add(interview)
    session.flush()
    for pos, text in enumerate(DEFAULT_QUESTIONS, start=1):
        session.add(InterviewQuestion(interview_id=interview.id, position=pos, prompt_text=text))
    session.flush()
    return interview


def _add_call(session: Session, cand: Candidate, interview: Interview,
              direction: CallDirection, status: CallStatus, provider_call_id: str) -> Call:
    call = Call(
        candidate_id=cand.id, interview_id=interview.id,
        direction=direction.value, status=status.value,
        provider_call_id=provider_call_id, transport="twilio",
        from_number=cand.phone,
    )
    session.add(call)
    session.flush()
    return call


def _answer_through(session: Session, interview: Interview, up_to_position: int, call: Call) -> None:
    qs = session.scalars(
        select(InterviewQuestion)
        .where(InterviewQuestion.interview_id == interview.id,
               InterviewQuestion.position <= up_to_position)
        .order_by(InterviewQuestion.position)
    ).all()
    for q in qs:
        session.add(InterviewAnswer(
            interview_id=interview.id, question_id=q.id, call_id=call.id,
            transcript=f"(seeded answer to Q{q.position})", asr_confidence=0.9,
        ))
    session.flush()


def build(session: Session, kind: str, *, name: str = "Rahul",
          phone: str = "+919000000001", email: str = "rahul@example.com",
          role: str = "Backend Engineer") -> dict:
    """kind: no-answer | voicemail | interrupted | completed | unknown-caller"""
    cand = _upsert_candidate(session, name, phone, email)
    interview = _fresh_interview(session, cand, role)
    sid = f"SCN{kind}{interview.id}"

    if kind == "no-answer":
        _add_call(session, cand, interview, CallDirection.OUTBOUND, CallStatus.NO_ANSWER, sid)

    elif kind == "voicemail":
        _add_call(session, cand, interview, CallDirection.OUTBOUND, CallStatus.VOICEMAIL, sid)

    elif kind == "interrupted":
        interview.status = InterviewStatus.INTERRUPTED.value
        interview.started_at = datetime.now(timezone.utc)
        interview.current_question = 4
        call = _add_call(session, cand, interview, CallDirection.OUTBOUND,
                         CallStatus.DISCONNECTED, sid)
        _answer_through(session, interview, 3, call)

    elif kind == "completed":
        interview.status = InterviewStatus.COMPLETED.value
        interview.started_at = datetime.now(timezone.utc)
        interview.completed_at = datetime.now(timezone.utc)
        interview.current_question = len(DEFAULT_QUESTIONS)
        call = _add_call(session, cand, interview, CallDirection.OUTBOUND,
                         CallStatus.COMPLETED, sid)
        _answer_through(session, interview, len(DEFAULT_QUESTIONS), call)

    elif kind == "unknown-caller":
        # Candidate exists but with a phone that won't match the inbound caller id.
        cand.phone = normalize_phone("+919999999999")
        _add_call(session, cand, interview, CallDirection.OUTBOUND, CallStatus.NO_ANSWER, sid)
        session.flush()

    else:
        raise ValueError(f"unknown scenario kind: {kind}")

    return {
        "scenario": kind,
        "candidate": {"id": cand.id, "name": cand.name, "phone": cand.phone},
        "interview": {"id": interview.id, "status": interview.status,
                      "current_question": interview.current_question, "role": interview.role,
                      "invitation_code": interview.invitation_code},
    }

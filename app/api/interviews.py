"""Interview read + orchestrated-action endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.auth import require_agent, require_recruiter
from app.api.schemas import AnswerRequest, CallScopedRequest, IntentRequest
from app.db.models import Interview
from app.db.session import get_session
from app.services import interview_service as iv
from app.services import interview_orchestrator as orch

router = APIRouter(prefix="/interviews", tags=["interviews"])


def _get(session: Session, interview_id: int) -> Interview:
    interview = session.get(Interview, interview_id)
    if interview is None:
        raise HTTPException(404, "interview not found")
    return interview


def _authorize_call(session: Session, call_id: int, interview_id: int) -> None:
    if not iv.call_owns_interview(session, call_id, interview_id):
        raise HTTPException(404, "interview not found")


@router.get("/{interview_id}")
def get_interview(interview_id: int, session: Session = Depends(get_session),
                  _=Depends(require_recruiter)) -> dict:
    interview = _get(session, interview_id)
    return {
        "id": interview.id, "candidate_id": interview.candidate_id,
        "role": interview.role, "status": interview.status,
        "current_question": interview.current_question,
    }


@router.get("/{interview_id}/state")
def get_state(interview_id: int, session: Session = Depends(get_session),
              _=Depends(require_recruiter)) -> dict:
    interview = _get(session, interview_id)
    return {
        "id": interview.id,
        "stored_status": interview.status,
        "effective_status": iv.effective_status(interview).value,
        "current_question": interview.current_question,
        "next_unanswered": iv.next_unanswered_position(session, interview),
    }


@router.post("/action")
def take_action(body: IntentRequest, session: Session = Depends(get_session),
                _=Depends(require_agent)) -> dict:
    """The single entry point the voice agent's tools call: propose an intent,
    get back the validated action + a message to speak."""
    _authorize_call(session, body.call_id, body.interview_id)
    result = orch.decide_and_execute(
        session, interview_id=body.interview_id, intent=body.intent, call_id=body.call_id,
    )
    return {
        "action": result.action.value,
        "allowed": result.allowed,
        "interview_id": result.interview_id,
        "status": result.status.value,
        "current_question": result.current_question,
        "current_question_text": result.current_question_text,
        "message": result.message,
    }


@router.post("/{interview_id}/answer")
def answer(interview_id: int, body: AnswerRequest,
           session: Session = Depends(get_session), _=Depends(require_agent)) -> dict:
    """Record the candidate's answer to the current question and advance. Returns
    the next question, or done=True when the interview completes."""
    _authorize_call(session, body.call_id, interview_id)
    res = orch.record_answer(session, interview_id=interview_id,
                             transcript=body.transcript, call_id=body.call_id)
    return {
        "done": res.done,
        "position": res.position,
        "next_question_text": res.next_question_text,
        "message": res.message,
    }


@router.post("/{interview_id}/complete")
def complete(interview_id: int, body: CallScopedRequest,
             session: Session = Depends(get_session), _=Depends(require_agent)) -> dict:
    _authorize_call(session, body.call_id, interview_id)
    orch.complete_interview(session, interview_id)
    return {"interview_id": interview_id, "status": "COMPLETED"}


@router.post("/{interview_id}/interrupt")
def interrupt(interview_id: int, body: CallScopedRequest,
              session: Session = Depends(get_session), _=Depends(require_agent)) -> dict:
    """Mark an in-progress interview INTERRUPTED (e.g. on call drop) so it can resume."""
    _authorize_call(session, body.call_id, interview_id)
    orch.mark_interrupted(session, interview_id)
    return {"interview_id": interview_id, "status": "INTERRUPTED"}


@router.post("/{interview_id}/evaluate")
def evaluate(interview_id: int, session: Session = Depends(get_session),
             _=Depends(require_recruiter)) -> dict:
    """Rubric-grounded scoring of all answered questions (internal/recruiter-facing)."""
    from app.services import evaluation_service
    return evaluation_service.evaluate_interview(session, interview_id)

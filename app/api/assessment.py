"""Assessment endpoints — role-based quiz with live, silent, graded validation."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db.session import get_session
from app.services import assessment_service as asv

router = APIRouter(prefix="/assessment", tags=["assessment"])


class StartRequest(BaseModel):
    role: str
    candidate_name: str | None = None
    call_id: int | None = None


class GradeRequest(BaseModel):
    session_id: int
    transcript: str


@router.get("/roles")
def roles(session: Session = Depends(get_session)) -> dict:
    return {"roles": asv.list_roles(session)}


@router.get("/roles/{role}/questions")
def questions(role: str, session: Session = Depends(get_session)) -> dict:
    qs = asv.get_questions(session, role)
    return {"role": role, "questions": [{"position": q.position, "prompt": q.prompt,
                                          "difficulty": q.difficulty} for q in qs]}


@router.post("/start")
def start(body: StartRequest, session: Session = Depends(get_session)) -> dict:
    return asv.start_session(session, body.role, candidate_name=body.candidate_name,
                             call_id=body.call_id)


@router.post("/grade")
def grade(body: GradeRequest, session: Session = Depends(get_session)) -> dict:
    """Grade the current answer silently and return the next question or the summary."""
    return asv.grade_answer(session, session_id=body.session_id, transcript=body.transcript)


@router.get("/{session_id}/summary")
def summary(session_id: int, session: Session = Depends(get_session)) -> dict:
    from app.db.models import AssessmentSession
    s = session.get(AssessmentSession, session_id)
    if s is None:
        return {"ok": False, "message": "Unknown session."}
    # Live aggregate (derived from persisted answers) plus the stamped record fields
    # (candidate_name + the rating written on completion) so the recruiter sees the
    # stored result. Read-only: _summary does not mutate when not just_graded.
    out = asv._summary(session, s, asv.get_questions(session, s.role))
    out["session_id"] = s.id
    out["candidate_name"] = s.candidate_name
    out["persisted"] = {
        "rating": s.rating, "overall_score": s.overall_score,
        "passed_count": s.passed_count, "answered": s.answered,
        "total_questions": s.total_questions,
        "completed_at": s.completed_at.isoformat() if s.completed_at else None,
    }
    return out

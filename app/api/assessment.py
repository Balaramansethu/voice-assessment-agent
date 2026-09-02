"""Assessment endpoints — role-based quiz with live, silent, graded validation."""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends
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
def grade(body: GradeRequest, background_tasks: BackgroundTasks,
          session: Session = Depends(get_session)) -> dict:
    """Submit the current answer and return the next question (or the done response)
    IMMEDIATELY. Silent grading is SCHEDULED to run in the background after the response
    is sent — it's recruiter-only, so it has no business on the conversation critical path.

    The sync endpoint runs in FastAPI's threadpool; BackgroundTasks fire after the
    response, and `grade_pending_answer` opens its own DB session (this request's session
    is closed by then). `_answer_id` is an internal handle — stripped before the response
    so the agent tool contract stays {next_question} / {done}."""
    result = asv.grade_answer(session, session_id=body.session_id, transcript=body.transcript)
    answer_id = result.pop("_answer_id", None)
    if answer_id is not None:
        background_tasks.add_task(asv.grade_pending_answer, answer_id)
    return result


@router.get("/by_call")
def by_call(provider_call_id: str, session: Session = Depends(get_session)) -> dict:
    """Resolve the assessment session opened against a given provider_call_id.

    Read-only, transition-free. The offline self-test harness opens a call with a unique
    provider_call_id, then needs the session id the agent bound server-side (which it
    never exposes). We map provider_call_id → Call.id → the newest AssessmentSession on
    that call. General-purpose recruiter-side lookup too (call recording ↔ result)."""
    from sqlalchemy import select
    from app.db.models import AssessmentSession, Call
    call_id = session.scalar(select(Call.id).where(Call.provider_call_id == provider_call_id))
    if call_id is None:
        return {"ok": False, "session_id": None, "message": "Unknown call."}
    sid = session.scalar(
        select(AssessmentSession.id).where(AssessmentSession.call_id == call_id)
        .order_by(AssessmentSession.id.desc()).limit(1)
    )
    return {"ok": sid is not None, "session_id": sid, "call_id": call_id}


@router.get("/{session_id}/answers")
def answers(session_id: int, session: Session = Depends(get_session)) -> dict:
    """Raw persisted answers for a session (position + transcript + score/rating).

    Read-only, transition-free. Exposed so the offline self-test harness — which runs
    in the agent container and has no DB driver — can assert answer ALIGNMENT
    (5 answers, positions 1..5, transcripts 1:1) over HTTP. Transcripts are the
    candidate's own spoken words, not grading output, so this reveals no silent verdict
    the agent shouldn't see (scores are already returned by /summary for the recruiter)."""
    from sqlalchemy import select
    from app.db.models import AssessmentAnswer, AssessmentSession
    s = session.get(AssessmentSession, session_id)
    if s is None:
        return {"ok": False, "message": "Unknown session."}
    rows = session.scalars(
        select(AssessmentAnswer).where(AssessmentAnswer.session_id == session_id)
        .order_by(AssessmentAnswer.position)
    ).all()
    return {"ok": True, "session_id": session_id,
            "answers": [{"position": r.position, "transcript": r.transcript,
                         "score": r.score, "rating": r.rating} for r in rows]}


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

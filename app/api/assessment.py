"""Assessment endpoints — role-based quiz with live, silent, graded validation."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session

from app.api.auth import require_agent, require_agent_or_recruiter, require_recruiter
from app.api.schemas import bounded_str
from app.db.models import Call
from app.db.session import get_session
from app.services import assessment_service as asv

router = APIRouter(prefix="/assessment", tags=["assessment"])

# NOTE: deliberately NOT stripped — a whitespace-only transcript ("   ") is a legitimate
# STT silence/babble artifact and must reach assessment_service.grade_answer's existing
# no-op handling (blank-transcript branch), not 422 here. See
# test_grade_request_accepts_whitespace_only_transcript.
Transcript = bounded_str(12_000, strip=False)
Role = bounded_str(200)
CandidateName = bounded_str(200)
# NOTE: max_length counts Unicode code points, not visual/grapheme characters — an
# NFD-decomposed name (base letter + combining marks) could hit this limit sooner than a
# candidate would expect. Documented limitation, not a functional fix.
ProviderCallId = bounded_str(128)


class StartRequest(BaseModel):
    role: Role
    candidate_name: CandidateName | None = None
    call_id: int

    @field_validator("candidate_name", mode="before")
    @classmethod
    def _blank_name_is_none(cls, v):
        if isinstance(v, str) and v.strip() == "":
            return None
        return v


class GradeRequest(BaseModel):
    session_id: int
    call_id: int
    transcript: Transcript


@router.get("/roles")
def roles(session: Session = Depends(get_session)) -> dict:
    return {"roles": asv.list_roles(session)}


@router.get("/roles/{role}/questions")
def questions(role: str, session: Session = Depends(get_session)) -> dict:
    qs = asv.get_questions(session, role)
    return {"role": role, "questions": [{"position": q.position, "prompt": q.prompt,
                                          "difficulty": q.difficulty} for q in qs]}


@router.post("/start")
def start(body: StartRequest, session: Session = Depends(get_session),
          _=Depends(require_agent)) -> dict:
    call = session.get(Call, body.call_id)
    if call is None:
        raise HTTPException(404, "call not found")
    return asv.start_session(session, body.role, candidate_name=body.candidate_name,
                             call_id=body.call_id, candidate_id=call.candidate_id,
                             interview_id=call.interview_id,
                             invitation_code_used=call.resolved_invitation_code)


@router.post("/grade")
def grade(body: GradeRequest, session: Session = Depends(get_session),
          _=Depends(require_agent)) -> dict:
    """Submit the current answer and return the next question (or the done response)
    IMMEDIATELY. Silent grading is DURABLE — each answer gets a GradingJob row committed
    in the same transaction as the answer, claimed independently by the separate `grading_worker`
    process. This is recruiter-only, so it has no business on the conversation critical path.

    The sync endpoint runs in FastAPI's threadpool. Grading happens asynchronously via the
    durable worker: it opens its own DB session and processes the job queue, shielding the
    worker from API process crashes. `_answer_id` is an internal handle — stripped before the
    response so the agent tool contract stays {next_question} / {done}."""
    if not asv.call_owns_session(session, body.session_id, body.call_id):
        raise HTTPException(404, "assessment session not found")
    result = asv.grade_answer(session, session_id=body.session_id, transcript=body.transcript)
    result.pop("_answer_id", None)  # strip internal handle
    return result


@router.get("/by_call")
def by_call(provider_call_id: ProviderCallId, session: Session = Depends(get_session),
            _=Depends(require_agent_or_recruiter)) -> dict:
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
def answers(session_id: int, session: Session = Depends(get_session),
            _=Depends(require_recruiter)) -> dict:
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
def summary(session_id: int, session: Session = Depends(get_session),
            _=Depends(require_recruiter)) -> dict:
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

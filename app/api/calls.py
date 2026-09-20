"""Inbound-call intake. Idempotent: creates the call record, resolves the
candidate, and returns the interview context the agent should open with."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path
from sqlalchemy.orm import Session

from app.api.auth import require_agent, require_recruiter
from app.api.schemas import (
    EndCallRequest, IdentifyRequest, InboundCallRequest, VerifyInvitationRequest
)
from app.db.models import Call, Candidate
from app.db.session import get_session
from app.domain.states import CallDirection, CallStatus, InterviewStatus
from app.services import call_service as cs
from app.services import candidate_resolver as cr
from app.services import interview_service as iv

router = APIRouter(prefix="/calls", tags=["calls"])


@router.post("/identify")
def identify(body: IdentifyRequest, session: Session = Depends(get_session),
             _=Depends(require_recruiter)) -> dict:
    """Resolve a candidate by spoken name and attach them to the live call. Returns
    the interview context the agent should open the screening with."""
    cand = cr.resolve_by_name(session, body.name)
    if cand is None:
        return {"resolved": False, "candidate": None, "interview": None}

    call = session.get(Call, body.call_id) if body.call_id else None
    if call is not None:
        call.candidate_id = cand.id
    interviews = iv.list_active_interviews(session, cand.id)
    iv.record_event(session, event_type="CANDIDATE_RESOLVED",
                    call_id=body.call_id, payload={"candidate_id": cand.id, "by": "name"})

    if not interviews:
        return {"resolved": True, "candidate": {"id": cand.id, "name": cand.name},
                "interview": None}
    if len(interviews) > 1:
        return {"resolved": True, "candidate": {"id": cand.id, "name": cand.name},
                "interview": None,
                "options": [{"id": i.id, "role": i.role} for i in interviews]}

    interview = interviews[0]
    if call is not None:
        call.interview_id = interview.id
    session.flush()
    return {
        "resolved": True,
        "candidate": {"id": cand.id, "name": cand.name},
        "interview": {"id": interview.id, "role": interview.role,
                      "status": iv.effective_status(interview).value,
                      "current_question": interview.current_question},
    }


@router.post("/inbound")
def inbound(body: InboundCallRequest, session: Session = Depends(get_session),
            _=Depends(require_agent)) -> dict:
    call = cs.ingest_call(session, provider_call_id=body.provider_call_id,
                          direction=CallDirection.INBOUND, status=CallStatus.ANSWERED,
                          transport=body.transport, from_number=body.from_number,
                          to_number=body.to_number)
    cand = cr.resolve_by_phone(session, body.from_number)
    interviews = iv.list_active_interviews(session, cand.id) if cand else []
    if cand is None or len(interviews) != 1:
        iv.record_event(session, event_type="INBOUND_CALL_RECEIVED", call_id=call.id,
                        payload={"resolved": False, "active_interviews": len(interviews)})
        return {"call_id": call.id, "resolved": False, "candidate": None,
                "interview": None, "prompt": "verify_invitation_code"}
    interview = interviews[0]
    call.candidate_id, call.interview_id = cand.id, interview.id
    session.flush()
    iv.record_event(session, event_type="CANDIDATE_RESOLVED", call_id=call.id,
                    payload={"candidate_id": cand.id, "by": "phone"})
    return {"call_id": call.id, "resolved": True,
            "candidate": {"id": cand.id, "name": cand.name},
            "interview": {"id": interview.id, "role": interview.role,
                          "status": iv.effective_status(interview).value,
                          "current_question": interview.current_question,
                          "next_unanswered": iv.next_unanswered_position(session, interview)},
            "candidate_name": cand.name, "prompt": "greet_with_context"}


_MAX_INVITATION_ATTEMPTS = 5


@router.post("/verify_invitation")
def verify_invitation(body: VerifyInvitationRequest, session: Session = Depends(get_session),
                       _=Depends(require_agent)) -> dict:
    call = session.get(Call, body.call_id)
    if call is None:
        raise HTTPException(404, "call not found")
    if iv.invitation_attempts_rejected(session, call.id) >= _MAX_INVITATION_ATTEMPTS:
        return {"resolved": False, "locked": True,
                "message": "Too many attempts — connecting you with the recruiting team."}
    interview = cr.resolve_by_invitation_code(session, body.code)
    if interview is None:
        iv.record_event(session, event_type="INVITATION_CODE_REJECTED", call_id=call.id,
                        payload={"reason": "not_found"})
        return {"resolved": False, "message": "That code wasn't recognized."}
    status = iv.effective_status(interview)
    if status in (InterviewStatus.EXPIRED, InterviewStatus.CANCELLED, InterviewStatus.COMPLETED):
        iv.record_event(session, event_type="INVITATION_CODE_REJECTED", call_id=call.id,
                        interview_id=interview.id,
                        payload={"reason": "inactive", "status": status.value})
        return {"resolved": False, "message": "That interview code is no longer active."}
    call.candidate_id = interview.candidate_id
    call.interview_id = interview.id
    call.resolved_invitation_code = interview.invitation_code
    session.flush()
    cand = session.get(Candidate, interview.candidate_id)
    iv.record_event(session, event_type="CANDIDATE_RESOLVED", call_id=call.id,
                    interview_id=interview.id, payload={"by": "invitation_code"})
    return {"resolved": True, "candidate": {"id": cand.id, "name": cand.name},
            "interview": {"id": interview.id, "role": interview.role,
                          "status": status.value,
                          "current_question": interview.current_question}}


@router.post("/{call_id}/end")
def end(
    call_id: Annotated[int, Path(ge=-2_147_483_648, le=2_147_483_647)],
    body: EndCallRequest,
    session: Session = Depends(get_session),
    _=Depends(require_agent),
) -> dict:
    """Stamp a terminal call status (idempotent — see call_service.end_call).
    Called by the agent's on_client_disconnected handler and the max-call-
    duration watchdog (PR-021); never by the LLM directly. The Path bounds
    match Postgres's int4 range for Call.id — without them, an absurdly
    large id reaches session.get() and raises an unhandled
    psycopg.errors.NumericValueOutOfRange (-> 500) before the 404 check
    below ever runs; 0/-1 are in-range and still 404 normally, unchanged."""
    call = session.get(Call, call_id)
    if call is None:
        raise HTTPException(404, "call not found")
    cs.end_call(session, call, body.status)
    return {"call_id": call.id, "status": call.status,
            "ended_at": call.ended_at.isoformat() if call.ended_at else None}

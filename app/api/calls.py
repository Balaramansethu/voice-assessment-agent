"""Inbound-call intake. Idempotent: creates the call record, resolves the
candidate, and returns the interview context the agent should open with."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.schemas import IdentifyRequest, InboundCallRequest
from app.db.models import Call
from app.db.session import get_session
from app.domain.states import CallDirection, CallStatus
from app.services import call_service as cs
from app.services import candidate_resolver as cr
from app.services import interview_service as iv

router = APIRouter(prefix="/calls", tags=["calls"])


@router.post("/identify")
def identify(body: IdentifyRequest, session: Session = Depends(get_session)) -> dict:
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
def inbound(body: InboundCallRequest, session: Session = Depends(get_session)) -> dict:
    call = cs.ingest_call(
        session,
        provider_call_id=body.provider_call_id,
        direction=CallDirection.INBOUND,
        status=CallStatus.ANSWERED,
        transport=body.transport,
        from_number=body.from_number,
        to_number=body.to_number,
    )

    cand = cr.resolve_by_phone(session, body.from_number)
    if cand is None:
        iv.record_event(session, event_type="INBOUND_CALL_RECEIVED", call_id=call.id,
                        payload={"resolved": False})
        return {"call_id": call.id, "resolved": False, "candidate": None,
                "interview": None,
                "prompt": "ask_for_identifier"}

    call.candidate_id = cand.id
    interviews = iv.list_active_interviews(session, cand.id)
    iv.record_event(session, event_type="CANDIDATE_RESOLVED", call_id=call.id,
                    payload={"candidate_id": cand.id, "active_interviews": len(interviews)})

    if len(interviews) == 0:
        return {"call_id": call.id, "resolved": True,
                "candidate": {"id": cand.id, "name": cand.name},
                "interview": None, "prompt": "no_active_interview"}

    if len(interviews) > 1:
        return {"call_id": call.id, "resolved": True,
                "candidate": {"id": cand.id, "name": cand.name},
                "interview": None, "prompt": "disambiguate",
                "options": [{"id": i.id, "role": i.role} for i in interviews]}

    interview = interviews[0]
    call.interview_id = interview.id
    session.flush()
    return {
        "call_id": call.id, "resolved": True,
        "candidate": {"id": cand.id, "name": cand.name},
        "interview": {
            "id": interview.id, "role": interview.role,
            "status": iv.effective_status(interview).value,
            "current_question": interview.current_question,
            "next_unanswered": iv.next_unanswered_position(session, interview),
        },
        "candidate_name": cand.name,
        "prompt": "greet_with_context",
    }

"""Candidate read + deterministic resolution endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.auth import require_recruiter
from app.api.schemas import ResolveRequest
from app.db.models import Candidate
from app.db.session import get_session
from app.services import candidate_resolver as cr
from app.services import interview_service as iv

router = APIRouter(prefix="/candidates", tags=["candidates"],
                   dependencies=[Depends(require_recruiter)])


@router.get("/{candidate_id}")
def get_candidate(candidate_id: int, session: Session = Depends(get_session)) -> dict:
    cand = session.get(Candidate, candidate_id)
    if cand is None:
        raise HTTPException(404, "candidate not found")
    return {"id": cand.id, "name": cand.name, "phone": cand.phone, "email": cand.email}


@router.post("/resolve")
def resolve(body: ResolveRequest, session: Session = Depends(get_session)) -> dict:
    """Resolve a caller to a candidate + their active interview. Phone first,
    identifier as the unknown-caller fallback. Never guesses."""
    cand = None
    if body.phone:
        cand = cr.resolve_by_phone(session, body.phone)
    if cand is None and body.identifier:
        cand = cr.resolve_by_identifier(session, body.identifier)

    if cand is None:
        return {"resolved": False, "candidate": None, "interviews": []}

    interviews = iv.list_active_interviews(session, cand.id)
    return {
        "resolved": True,
        "candidate": {"id": cand.id, "name": cand.name},
        "interviews": [
            {"id": i.id, "role": i.role, "status": iv.effective_status(i).value}
            for i in interviews
        ],
    }

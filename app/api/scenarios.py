"""Scenario simulator endpoints — POST to seed any candidate state in seconds."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.schemas import ScenarioRequest
from app.db.session import get_session
from app.services import scenario_service

router = APIRouter(prefix="/scenarios", tags=["scenarios"])

_KINDS = ["no-answer", "voicemail", "interrupted", "completed", "unknown-caller"]


def _make(kind: str):
    def handler(body: ScenarioRequest = ScenarioRequest(),
                session: Session = Depends(get_session)) -> dict:
        return scenario_service.build(
            session, kind, name=body.name, phone=body.phone,
            email=body.email, role=body.role,
        )
    return handler


for _kind in _KINDS:
    router.add_api_route(f"/{_kind}", _make(_kind), methods=["POST"], name=f"scenario_{_kind}")

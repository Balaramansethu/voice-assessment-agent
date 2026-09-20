"""P1 authorization tests (PR-108): cross-candidate, cross-session, altered-ID,
expired-invitation, and ambiguous-phone. Run over a real Postgres:
    docker compose exec api pytest tests/integration/test_authorization.py
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import func, select

import app.main as main_module
from app.config import settings
from app.db.models import (
    AssessmentSession,
    Call,
    Candidate,
    Interview,
    InterviewAnswer,
)
from app.db.session import SessionLocal, init_db
from app.domain.states import CallDirection, CallStatus, Intent
from app.services import assessment_service as asv
from app.services import interview_orchestrator as orch

client = TestClient(main_module.app)

_AGENT = {"X-Agent-Key": settings.agent_shared_key}
_RECRUITER = {"X-Recruiter-Key": settings.recruiter_shared_key}

# Per-test-session uniqueness so reruns against the same (untruncated) Postgres never
# collide on Candidate.phone's unique constraint or Call.provider_call_id's.
_RUN_ID = os.urandom(4).hex()


def _session():
    init_db()
    return SessionLocal()


def _make_candidate_interview_call(s, *, phone: str, role: str = "Backend Engineer",
                                   expires_in_hours: int | None = 72) -> tuple[Candidate, Interview, Call]:
    cand = Candidate(name=f"Test {phone}", phone=phone)
    s.add(cand)
    s.flush()
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=expires_in_hours)
                 if expires_in_hours is not None else None)
    interview = Interview(candidate_id=cand.id, role=role,
                          invitation_code=orch.generate_invitation_code(),
                          expires_at=expires_at)
    s.add(interview)
    s.flush()
    call = Call(direction=CallDirection.INBOUND.value, status=CallStatus.ANSWERED.value,
               provider_call_id=f"CATEST{cand.id}{interview.id}", transport="webrtc",
               from_number=phone)
    s.add(call)
    s.flush()
    s.commit()
    return cand, interview, call


def _bind_call_via_invitation(s, call: Call, interview: Interview) -> None:
    resp = client.post("/calls/verify_invitation",
                       json={"call_id": call.id, "code": interview.invitation_code},
                       headers=_AGENT)
    assert resp.status_code == 200
    assert resp.json()["resolved"] is True
    s.expire_all()


# ---- Cross-candidate ----

def test_candidate_get_by_id_requires_recruiter_scope():
    s = _session()
    cand, _, _ = _make_candidate_interview_call(s, phone=f"+15551110001{_RUN_ID}")
    resp = client.get(f"/candidates/{cand.id}")
    assert resp.status_code == 401


def test_candidate_a_call_cannot_answer_candidate_b_interview():
    s = _session()
    _, interview_a, call_a = _make_candidate_interview_call(s, phone=f"+15551110002{_RUN_ID}")
    _bind_call_via_invitation(s, call_a, interview_a)
    _, interview_b, call_b = _make_candidate_interview_call(s, phone=f"+15551110003{_RUN_ID}")
    _bind_call_via_invitation(s, call_b, interview_b)

    before = s.scalar(select(func.count(InterviewAnswer.id))
                      .where(InterviewAnswer.interview_id == interview_b.id))

    resp = client.post(f"/interviews/{interview_b.id}/answer",
                       json={"transcript": "stolen answer", "call_id": call_a.id},
                       headers=_AGENT)
    assert resp.status_code == 404

    after = s.scalar(select(func.count(InterviewAnswer.id))
                     .where(InterviewAnswer.interview_id == interview_b.id))
    assert after == before


# ---- Cross-session ----

def test_call_a_cannot_grade_into_call_bs_assessment_session():
    s = _session()
    _, _, call_a = _make_candidate_interview_call(s, phone=f"+15551110004{_RUN_ID}")
    _, _, call_b = _make_candidate_interview_call(s, phone=f"+15551110005{_RUN_ID}")

    resp = client.post("/assessment/start",
                       json={"role": "Backend Engineer", "call_id": call_b.id},
                       headers=_AGENT)
    assert resp.status_code == 200
    session_id_b = resp.json()["session_id"]

    grade_resp = client.post("/assessment/grade",
                             json={"session_id": session_id_b, "call_id": call_a.id,
                                   "transcript": "attempted cross-call grade"},
                             headers=_AGENT)
    assert grade_resp.status_code == 404


# ---- Altered-ID ----

def test_incrementing_interview_id_in_action_request_is_rejected():
    s = _session()
    _, interview_a, call_a = _make_candidate_interview_call(s, phone=f"+15551110006{_RUN_ID}")
    _bind_call_via_invitation(s, call_a, interview_a)
    # A second, real interview that exists but isn't owned by call_a.
    _, interview_other, _ = _make_candidate_interview_call(s, phone=f"+15551110007{_RUN_ID}")

    resp = client.post("/interviews/action",
                       json={"interview_id": interview_other.id,
                             "intent": Intent.CONTINUE_INTERVIEW.value,
                             "call_id": call_a.id},
                       headers=_AGENT)
    assert resp.status_code == 404


# ---- Expired-invitation ----

def test_expired_invitation_code_is_rejected():
    s = _session()
    _, interview, call = _make_candidate_interview_call(
        s, phone=f"+15551110008{_RUN_ID}", expires_in_hours=-1)  # already expired

    resp = client.post("/calls/verify_invitation",
                       json={"call_id": call.id, "code": interview.invitation_code},
                       headers=_AGENT)
    assert resp.status_code == 200
    assert resp.json()["resolved"] is False

    s.expire_all()
    refreshed = s.get(Call, call.id)
    assert refreshed.candidate_id is None
    assert refreshed.interview_id is None


def test_invitation_code_lockout_after_five_failures():
    s = _session()
    _, interview, call = _make_candidate_interview_call(s, phone=f"+15551110009{_RUN_ID}")

    for _ in range(5):
        resp = client.post("/calls/verify_invitation",
                           json={"call_id": call.id, "code": "WRONGCODE"},
                           headers=_AGENT)
        assert resp.status_code == 200
        assert resp.json()["resolved"] is False

    # 6th attempt, this time with the GENUINELY correct code — must still be locked out.
    resp = client.post("/calls/verify_invitation",
                       json={"call_id": call.id, "code": interview.invitation_code},
                       headers=_AGENT)
    assert resp.status_code == 200
    assert resp.json().get("locked") is True

    s.expire_all()
    refreshed = s.get(Call, call.id)
    assert refreshed.candidate_id is None
    assert refreshed.interview_id is None


# ---- Ambiguous-phone ----

def test_ambiguous_active_interviews_forces_invitation_code_challenge():
    s = _session()
    phone = f"+15551110010{_RUN_ID}"
    cand = Candidate(name="Ambiguous Candidate", phone=phone)
    s.add(cand)
    s.flush()
    interview_1 = Interview(candidate_id=cand.id, role="Backend Engineer",
                            invitation_code=orch.generate_invitation_code())
    interview_2 = Interview(candidate_id=cand.id, role="Frontend Engineer",
                            invitation_code=orch.generate_invitation_code())
    s.add_all([interview_1, interview_2])
    s.commit()

    resp = client.post("/calls/inbound",
                       json={"provider_call_id": f"CAAMBIG1{_RUN_ID}", "from_number": phone,
                             "transport": "webrtc"},
                       headers=_AGENT)
    assert resp.status_code == 200
    body = resp.json()
    assert body["prompt"] == "verify_invitation_code"

    s.expire_all()
    call = s.scalar(select(Call).where(Call.provider_call_id == f"CAAMBIG1{_RUN_ID}"))
    assert call.candidate_id is None
    assert call.interview_id is None

    # The invitation code for interview_2 specifically must resolve interview_2,
    # not interview_1 — proves per-interview codes correctly disambiguate.
    verify = client.post("/calls/verify_invitation",
                         json={"call_id": call.id, "code": interview_2.invitation_code},
                         headers=_AGENT)
    assert verify.status_code == 200
    assert verify.json()["interview"]["id"] == interview_2.id


# ---- PR-104: identity persists end-to-end on the invitation-code path ----

def test_assessment_session_persists_full_identity_on_invitation_path():
    s = _session()
    cand, interview, call = _make_candidate_interview_call(s, phone=f"+15551110011{_RUN_ID}")
    _bind_call_via_invitation(s, call, interview)

    resp = client.post("/assessment/start",
                       json={"role": "Backend Engineer", "call_id": call.id},
                       headers=_AGENT)
    assert resp.status_code == 200
    session_id = resp.json()["session_id"]

    s.expire_all()
    row = s.get(AssessmentSession, session_id)
    assert row.candidate_id == cand.id
    assert row.interview_id == interview.id
    assert row.invitation_code_used == interview.invitation_code

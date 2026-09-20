"""P2 lifecycle tests: concurrent-submit race (PR-203), one-active-call-per-interview
(PR-204), interrupt-on-disconnect (PR-209). Run over a real Postgres:
    docker compose exec api pytest tests/integration/test_lifecycle.py
"""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

import app.main as main_module
from app.config import settings
from app.db.models import AssessmentAnswer, AssessmentSession, Call, Candidate, Interview
from app.db.session import SessionLocal, init_db
from app.domain.states import CallDirection, CallStatus
from app.services import assessment_service as asv
from app.services import call_service as cs
from app.services import interview_orchestrator as orch

client = TestClient(main_module.app)
_AGENT = {"X-Agent-Key": settings.agent_shared_key}
_RUN_ID = os.urandom(4).hex()


def _session():
    init_db()
    return SessionLocal()


def test_concurrent_grade_answer_calls_never_duplicate_a_position():
    """PR-203: two genuinely concurrent submits for the SAME session must not both
    land at the same question position. Before the row-lock fix, both could read the
    same answered-count and insert two AssessmentAnswer rows for the same position —
    real threads/sessions, not sequential calls, are required to exercise this."""
    s = _session()
    start = asv.start_session(s, "Backend Engineer", candidate_name=f"Race Test {_RUN_ID}")
    s.commit()
    sid = start["session_id"]

    def _submit(i):
        session = SessionLocal()
        try:
            return asv.grade_answer(session, session_id=sid, transcript=f"answer variant {i}")
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=5) as ex:
        [f.result() for f in [ex.submit(_submit, i) for i in range(5)]]

    s2 = _session()
    positions = s2.scalars(
        select(AssessmentAnswer.position).where(AssessmentAnswer.session_id == sid)
    ).all()
    assert len(positions) == len(set(positions)), f"duplicate positions recorded: {positions}"


def test_one_active_call_per_interview_enforced_at_db_level():
    """PR-204: a second ACTIVE call for the same interview must be rejected
    by the database constraint. This constraint prevents the race where
    two concurrent sessions both read the same interview_id and both try
    to set status='ACTIVE'."""
    s = _session()
    cand = Candidate(name=f"DBIndex {_RUN_ID}", phone=f"+1555222{_RUN_ID[:4]}")
    s.add(cand)
    s.flush()
    interview = Interview(candidate_id=cand.id, role="Backend Engineer",
                          invitation_code=orch.generate_invitation_code())
    s.add(interview)
    s.flush()
    call_1 = Call(direction=CallDirection.INBOUND.value, status=CallStatus.ACTIVE.value,
                  provider_call_id=f"CALIFE1{_RUN_ID}", transport="webrtc",
                  interview_id=interview.id, candidate_id=cand.id)
    s.add(call_1)
    s.commit()

    # Try to create a second ACTIVE call for the same interview
    call_2 = Call(direction=CallDirection.INBOUND.value, status=CallStatus.ACTIVE.value,
                  provider_call_id=f"CALIFE2{_RUN_ID}", transport="webrtc",
                  interview_id=interview.id, candidate_id=cand.id)
    s.add(call_2)
    raised = False
    try:
        s.commit()
    except IntegrityError:
        raised = True
        s.rollback()
    assert raised, "a second ACTIVE call for the same interview must violate DB constraint"

    cs.end_call(s, call_1, CallStatus.COMPLETED)  # avoid polluting later runs' ceiling
    s.commit()


def test_interview_interrupt_endpoint_still_works_directly():
    """Sanity check the /interviews/{id}/interrupt endpoint PR-209 now calls
    from the disconnect handler — not a pipeline-level test (pipecat isn't
    importable in this image), just confirms the HTTP contract mark_interrupted()
    depends on is intact."""
    s = _session()
    cand = Candidate(name=f"Interrupt Test {_RUN_ID}", phone=f"+1555333{_RUN_ID[:4]}")
    s.add(cand)
    s.flush()
    interview = Interview(candidate_id=cand.id, role="Backend Engineer", status="IN_PROGRESS",
                          invitation_code=orch.generate_invitation_code())
    s.add(interview)
    s.flush()
    call = Call(direction=CallDirection.INBOUND.value, status=CallStatus.ACTIVE.value,
               provider_call_id=f"CAINTERRUPT1{_RUN_ID}", transport="webrtc",
               interview_id=interview.id, candidate_id=cand.id)
    s.add(call)
    s.commit()

    resp = client.post(f"/interviews/{interview.id}/interrupt",
                       json={"call_id": call.id}, headers=_AGENT)
    assert resp.status_code == 200

    s.expire_all()
    refreshed = s.get(Interview, interview.id)
    assert refreshed.status == "INTERRUPTED"

    cs.end_call(s, call, CallStatus.COMPLETED)  # avoid polluting later runs' ceiling
    s.commit()

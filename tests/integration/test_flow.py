"""End-to-end business flow over a real Postgres (via the running api container's
DB). Run inside the container: `docker compose exec api pytest tests/integration`.
"""
from app.db.session import SessionLocal, init_db
from app.domain.states import Intent, InterviewStatus
from app.services import interview_service as iv
from app.services import interview_orchestrator as orch
from app.services import scenario_service


def _session():
    init_db()
    return SessionLocal()


def test_no_answer_then_start():
    s = _session()
    info = scenario_service.build(s, "no-answer", phone="+919000000010")
    s.commit()
    iid = info["interview"]["id"]
    res = orch.decide_and_execute(s, interview_id=iid, intent=Intent.START_INTERVIEW)
    s.commit()
    assert res.allowed and res.action.value == "START"
    assert res.status == InterviewStatus.IN_PROGRESS
    assert res.current_question == 1


def test_interrupted_resumes_at_next_unanswered():
    s = _session()
    info = scenario_service.build(s, "interrupted", phone="+919000000011")
    s.commit()
    iid = info["interview"]["id"]
    res = orch.decide_and_execute(s, interview_id=iid, intent=Intent.CONTINUE_INTERVIEW)
    s.commit()
    assert res.action.value == "RESUME"
    assert res.current_question == 4  # Q1-3 seeded as answered


def test_resume_then_answer_loop_completes():
    s = _session()
    info = scenario_service.build(s, "interrupted", phone="+919000000021")
    s.commit()
    iid = info["interview"]["id"]
    res = orch.decide_and_execute(s, interview_id=iid, intent=Intent.CONTINUE_INTERVIEW)
    s.commit()
    assert res.action.value == "RESUME"
    assert res.current_question_text  # backend hands the agent the actual question
    # answer the remaining questions until the interview completes
    done = False
    for _ in range(20):  # guard against runaway
        r = orch.record_answer(s, interview_id=iid, transcript="an answer"); s.commit()
        if r.done:
            done = True
            break
    assert done
    from app.db.models import Interview
    assert s.get(Interview, iid).status == InterviewStatus.COMPLETED.value


def test_completed_is_not_restarted():
    s = _session()
    info = scenario_service.build(s, "completed", phone="+919000000012")
    s.commit()
    iid = info["interview"]["id"]
    res = orch.decide_and_execute(s, interview_id=iid, intent=Intent.START_INTERVIEW)
    s.commit()
    assert not res.allowed and res.action.value == "REJECT"


def test_effective_status_derives_expired(monkeypatch):
    from datetime import datetime, timedelta, timezone
    s = _session()
    info = scenario_service.build(s, "no-answer", phone="+919000000013")
    interview = s.get(type(iv).__globals__["Interview"], info["interview"]["id"]) \
        if False else None
    # simpler: fetch via service
    from app.db.models import Interview
    interview = s.get(Interview, info["interview"]["id"])
    interview.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
    s.commit()
    assert iv.effective_status(interview) == InterviewStatus.EXPIRED

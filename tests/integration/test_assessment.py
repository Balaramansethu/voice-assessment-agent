"""Assessment flow over a real Postgres: name + overall rating persistence.

Grading itself calls Groq; here we monkeypatch the LLM judge (`_grade`) with a
deterministic stub so the test is hermetic and asserts the state/persistence
behaviour, not the model. Run inside the api container:
    docker compose exec api pytest tests/integration/test_assessment.py
"""
from app.db.models import AssessmentSession
from app.db.session import SessionLocal, init_db
from app.services import assessment_service as asv


def _session():
    init_db()
    return SessionLocal()


def _drive_to_completion(s, session_id, scores):
    """Grade every question, feeding one stubbed score per call, return last result."""
    res = None
    for _ in scores:
        res = asv.grade_answer(s, session_id=session_id, transcript="an answer")
        s.commit()
    return res


def test_rating_band_thresholds():
    # strong >= 0.7, mixed 0.4..0.69, weak < 0.4, None passthrough
    assert asv._rating_band(1.0) == "strong"
    assert asv._rating_band(0.7) == "strong"
    assert asv._rating_band(0.69) == "mixed"
    assert asv._rating_band(0.4) == "mixed"
    assert asv._rating_band(0.39) == "weak"
    assert asv._rating_band(0.0) == "weak"
    assert asv._rating_band(None) is None


def test_name_and_rating_persist_on_completion(monkeypatch):
    s = _session()
    start = asv.start_session(s, "Backend Engineer", candidate_name="Grace Hopper")
    s.commit()
    sid = start["session_id"]
    total = start["total_questions"]

    # Deterministic judge: alternate strong/weak so the average is well-defined.
    scores = [1.0, 0.0] * total
    scores = scores[:total]
    it = iter(scores)

    def _stub_grade(prompt, expected, key_points, answer):
        sc = next(it)
        verdict = "correct" if sc >= 0.7 else "partial" if sc >= 0.4 else "incorrect"
        return {"verdict": verdict, "score": sc, "covered": [], "missing": [],
                "rationale": "stub"}

    monkeypatch.setattr(asv, "_grade", _stub_grade)

    res = _drive_to_completion(s, sid, scores)
    assert res["done"] is True

    # The persisted row carries the name AND the derived aggregate rating.
    row = s.get(AssessmentSession, sid)
    s.refresh(row)
    assert row.candidate_name == "Grace Hopper"
    assert row.completed_at is not None
    assert row.total_questions == total
    assert row.answered == total
    expected_avg = round(sum(scores) / len(scores), 2)
    assert row.average_score == expected_avg
    assert row.correct_count == sum(1 for sc in scores if sc >= 0.7)
    assert row.rating == asv._rating_band(expected_avg)

    # The persisted values agree with the live-derived summary (single scoring path).
    summary = asv._summary(s, row, asv.get_questions(s, row.role))
    assert summary["average_score"] == row.average_score
    assert summary["rating"] == row.rating
    assert summary["candidate_name"] == row.candidate_name


def test_summary_is_read_only_when_not_just_graded(monkeypatch):
    """The summary endpoint path must not stamp/mutate a session it merely reads."""
    s = _session()
    start = asv.start_session(s, "Backend Engineer", candidate_name="Ada")
    s.commit()
    sid = start["session_id"]
    row = s.get(AssessmentSession, sid)

    # No answers graded yet: reading a summary leaves the aggregate NULL.
    asv._summary(s, row, asv.get_questions(s, row.role))
    s.commit()
    s.refresh(row)
    assert row.rating is None
    assert row.completed_at is None

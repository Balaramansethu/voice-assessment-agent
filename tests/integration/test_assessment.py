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


def _stub_grade_for(scores):
    """Return a deterministic `_grade` replacement that yields the given 0..10 scores
    in order, shaped exactly like the real grader's output (0..10 rubric)."""
    it = iter(scores)

    def _stub(prompt, expected, key_points, answer):
        score = float(next(it))
        return {
            "score": score,
            "rating": asv._rating_band(score),
            "passed": score >= asv.PASS_THRESHOLD,
            "reason": "stub",
            "required_covered": [], "important_covered": [],
            "important_missed": [], "optional_missed": [], "technical_errors": [],
        }

    return _stub


def test_rating_band_thresholds():
    # 0..10 client-spec bands: Excellent>=9, Strong>=8, Good>=7, Partial>=5,
    # Weak>=3, Incorrect<3; None passthrough.
    assert asv._rating_band(10.0) == "Excellent"
    assert asv._rating_band(9.0) == "Excellent"
    assert asv._rating_band(8.0) == "Strong"
    assert asv._rating_band(7.0) == "Good"
    assert asv._rating_band(6.9) == "Partial"
    assert asv._rating_band(5.0) == "Partial"
    assert asv._rating_band(3.0) == "Weak"
    assert asv._rating_band(2.9) == "Incorrect"
    assert asv._rating_band(0.0) == "Incorrect"
    assert asv._rating_band(None) is None


def test_name_and_rating_persist_on_completion(monkeypatch):
    s = _session()
    start = asv.start_session(s, "Backend Engineer", candidate_name="Grace Hopper")
    s.commit()
    sid = start["session_id"]
    total = start["total_questions"]

    # Deterministic judge: alternate strong/weak so the average is well-defined and
    # no live Groq call is made.
    scores = ([9.0, 4.0] * total)[:total]
    monkeypatch.setattr(asv, "_grade", _stub_grade_for(scores))

    res = None
    for _ in scores:
        res = asv.grade_answer(s, session_id=sid, transcript="an answer")
        s.commit()
    assert res["done"] is True

    # The persisted row carries the name AND the derived aggregate rating.
    row = s.get(AssessmentSession, sid)
    s.refresh(row)
    assert row.candidate_name == "Grace Hopper"
    assert row.completed_at is not None
    assert row.total_questions == total
    assert row.answered == total
    expected_overall = round(sum(scores) / len(scores), 1)
    assert row.overall_score == expected_overall
    assert row.passed_count == sum(1 for sc in scores if sc >= asv.PASS_THRESHOLD)
    assert row.rating == asv._rating_band(expected_overall)

    # The persisted values agree with the live-derived summary (single scoring path).
    summary = asv._summary(s, row, asv.get_questions(s, row.role))
    assert summary["overall_score"] == row.overall_score
    assert summary["overall_rating"] == row.rating
    assert summary["candidate_name"] == row.candidate_name


def test_summary_is_read_only_when_not_just_graded():
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

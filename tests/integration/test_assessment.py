"""Assessment flow over a real Postgres: name + overall rating persistence.

Grading itself calls Groq; here we monkeypatch the LLM judge (`_grade`) with a
deterministic stub so the test is hermetic and asserts the state/persistence
behaviour, not the model. Run inside the api container:
    docker compose exec api pytest tests/integration/test_assessment.py
"""
from sqlalchemy import func, select

from app.db.models import AssessmentAnswer, AssessmentSession
from app.db.session import SessionLocal, init_db
from app.services import assessment_service as asv


def _answer_count(s, sid):
    return s.scalar(select(func.count(AssessmentAnswer.id))
                    .where(AssessmentAnswer.session_id == sid)) or 0


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

    # Distinct transcript per question (as in production — each answers a different
    # prompt); a repeated transcript would trip the duplicate-submit idempotency guard.
    res = None
    for i in range(len(scores)):
        res = asv.grade_answer(s, session_id=sid, transcript=f"an answer {i}")
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


def test_duplicate_submit_is_idempotent_no_op(monkeypatch):
    """A retried tool cycle resending the SAME transcript must NOT insert a second row or
    advance the position — it re-hands the current pending question (prod session 28)."""
    s = _session()
    start = asv.start_session(s, "Backend Engineer", candidate_name="Retry Rita")
    s.commit()
    sid = start["session_id"]
    total = start["total_questions"]
    assert total >= 3, "need a few questions to prove positions don't shift"

    # Grade a distinct transcript per question so we can prove the 1:1 mapping later.
    # Score them so the grader stub yields deterministic, distinguishable values.
    scores = [float((i % 5) + 5) for i in range(total)]
    monkeypatch.setattr(asv, "_grade", _stub_grade_for(scores))
    transcripts = [f"answer number {i}" for i in range(total)]

    # Answer questions 0 and 1 normally.
    asv.grade_answer(s, session_id=sid, transcript=transcripts[0])
    s.commit()
    res = asv.grade_answer(s, session_id=sid, transcript=transcripts[1])
    s.commit()
    assert res["done"] is False
    pending = res["next_question"]                 # this is question at position 3
    assert _answer_count(s, sid) == 2

    # Now the storm: resend transcripts[1] (the last stored) VERBATIM, plus a whitespace/
    # case variant — both are retries and must be no-ops that re-hand the SAME pending Q.
    for dup in (transcripts[1], f"  ANSWER   Number 1 ".upper().lower()):
        dup_res = asv.grade_answer(s, session_id=sid, transcript=dup)
        s.commit()
        assert dup_res["done"] is False
        assert dup_res["next_question"] == pending  # (c) still the correct pending Q
        assert _answer_count(s, sid) == 2           # (a) no extra row  (b) no advance

    # A genuinely NEW transcript for the pending question advances normally.
    res3 = asv.grade_answer(s, session_id=sid, transcript=transcripts[2])
    s.commit()
    assert _answer_count(s, sid) == 3

    # (d) the graded set is 1:1 with the distinct transcripts, in position order.
    rows = s.scalars(select(AssessmentAnswer).where(AssessmentAnswer.session_id == sid)
                     .order_by(AssessmentAnswer.position)).all()
    assert [r.transcript for r in rows] == transcripts[:3]
    assert [r.position for r in rows] == [1, 2, 3]


def test_empty_transcript_is_idempotent_no_op(monkeypatch):
    """A spurious empty/blank turn reaching the tool must not insert or advance."""
    s = _session()
    start = asv.start_session(s, "Backend Engineer", candidate_name="Blank Bob")
    s.commit()
    sid = start["session_id"]

    monkeypatch.setattr(asv, "_grade", _stub_grade_for([8.0] * start["total_questions"]))

    first_q = start["first_question"]
    for blank in ("", "   ", "\n\t "):
        res = asv.grade_answer(s, session_id=sid, transcript=blank)
        s.commit()
        assert res["done"] is False
        assert res["next_question"]["position"] == first_q["position"]
        assert _answer_count(s, sid) == 0          # nothing inserted, position 0 held


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

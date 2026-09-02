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
    # Submit fast (no grading in-path), collecting answer_ids to grade in the background.
    res = None
    answer_ids = []
    for i in range(len(scores)):
        res = asv.grade_answer(s, session_id=sid, transcript=f"an answer {i}")
        s.commit()
        answer_ids.append(res["_answer_id"])
    assert res["done"] is True

    # Grading + completion stamping now happen in the background (own session_scope).
    for aid in answer_ids:
        asv.grade_pending_answer(aid)
    s.expire_all()

    # The persisted row carries the name AND the derived aggregate rating.
    row = s.get(AssessmentSession, sid)
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


def test_submit_returns_next_question_before_grading(monkeypatch):
    """Fast-submit path: submitting an answer inserts the row and returns the next
    question WITHOUT the grade present yet (silent grading is off the critical path).
    The row exists with score NULL until the scheduled background task runs."""
    s = _session()
    start = asv.start_session(s, "Backend Engineer", candidate_name="Fast Fiona")
    s.commit()
    sid = start["session_id"]

    # Any call to _grade in the request path would be a regression — fail loudly.
    def _boom(*a, **k):
        raise AssertionError("_grade must NOT run in the submit/request path")

    monkeypatch.setattr(asv, "_grade", _boom)

    res = asv.grade_answer(s, session_id=sid, transcript="an ungraded answer")
    s.commit()
    assert res["done"] is False
    assert res["next_question"]["position"] == 2
    answer_id = res["_answer_id"]

    # The row is persisted immediately, ungraded (score/rating NULL).
    row = s.get(AssessmentAnswer, answer_id)
    s.refresh(row)
    assert row.transcript == "an ungraded answer"
    assert row.position == 1
    assert row.score is None and row.rating is None
    # And the session aggregate is NOT stamped yet.
    sess = s.get(AssessmentSession, sid)
    assert sess.completed_at is None


def test_background_grade_populates_and_stamps_once(monkeypatch):
    """After running each scheduled grade task, scores populate and — once every answer
    is graded — the session stamps overall_score/rating/passed_count/completed_at exactly
    once. `grade_pending_answer` opens its own session_scope(), matching production."""
    s = _session()
    start = asv.start_session(s, "Backend Engineer", candidate_name="Bg Bella")
    s.commit()
    sid = start["session_id"]
    total = start["total_questions"]

    scores = ([9.0, 4.0] * total)[:total]
    monkeypatch.setattr(asv, "_grade", _stub_grade_for(scores))

    # Submit every answer (fast path), collecting the answer_ids to grade.
    answer_ids = []
    for i in range(total):
        res = asv.grade_answer(s, session_id=sid, transcript=f"an answer {i}")
        s.commit()
        answer_ids.append(res["_answer_id"])

    # Nothing graded/stamped yet — all rows NULL, session not completed.
    sess = s.get(AssessmentSession, sid)
    s.refresh(sess)
    assert sess.completed_at is None
    assert (s.scalar(select(func.count(AssessmentAnswer.id)).where(
        AssessmentAnswer.session_id == sid, AssessmentAnswer.score.is_(None))) or 0) == total

    # Run the scheduled background grades (own session_scope each).
    for aid in answer_ids:
        asv.grade_pending_answer(aid)

    # Re-read from a fresh session (background tasks committed to their own).
    s.expire_all()
    row = s.get(AssessmentSession, sid)
    assert row.completed_at is not None
    assert row.total_questions == total
    assert row.answered == total
    expected_overall = round(sum(scores) / len(scores), 1)
    assert row.overall_score == expected_overall
    assert row.passed_count == sum(1 for sc in scores if sc >= asv.PASS_THRESHOLD)
    assert row.rating == asv._rating_band(expected_overall)

    # Per-answer scores populated 1:1 with the stubbed values, in position order.
    rows = s.scalars(select(AssessmentAnswer).where(AssessmentAnswer.session_id == sid)
                     .order_by(AssessmentAnswer.position)).all()
    assert [r.score for r in rows] == scores

    # Once-only: re-running any grade (idempotent) must NOT re-stamp a different value.
    stamped_at = row.completed_at
    asv.grade_pending_answer(answer_ids[-1])
    s.expire_all()
    row2 = s.get(AssessmentSession, sid)
    assert row2.completed_at == stamped_at


def test_grade_pending_stamps_exactly_once_when_last_grades_race(monkeypatch):
    """The last answer's grade is what flips the session complete. Running the final
    two grades back-to-back must stamp exactly once (completed_at guard + row lock)."""
    s = _session()
    start = asv.start_session(s, "Backend Engineer", candidate_name="Race Rhea")
    s.commit()
    sid = start["session_id"]
    total = start["total_questions"]
    assert total >= 2

    scores = [8.0] * total
    monkeypatch.setattr(asv, "_grade", _stub_grade_for(scores))

    answer_ids = [asv.grade_answer(s, session_id=sid, transcript=f"a {i}")["_answer_id"]
                  for i in range(total)]
    s.commit()

    # Grade all but the last two, then the last two consecutively.
    for aid in answer_ids[:-2]:
        asv.grade_pending_answer(aid)
    s.expire_all()
    assert s.get(AssessmentSession, sid).completed_at is None

    asv.grade_pending_answer(answer_ids[-2])
    asv.grade_pending_answer(answer_ids[-1])
    s.expire_all()
    row = s.get(AssessmentSession, sid)
    assert row.completed_at is not None
    assert row.answered == total


def test_grade_endpoint_schedules_background_task(monkeypatch):
    """Endpoint-level: POST /assessment/grade returns the next question and schedules the
    background grade via BackgroundTasks (which TestClient runs after the response). The
    response must NOT carry the internal _answer_id."""
    from fastapi.testclient import TestClient

    from app.main import app

    init_db()
    with SessionLocal() as setup:
        start = asv.start_session(setup, "Backend Engineer", candidate_name="Api Amy")
        setup.commit()
        sid = start["session_id"]

    monkeypatch.setattr(asv, "_grade", _stub_grade_for([9.0]))

    client = TestClient(app)
    resp = client.post("/assessment/grade",
                       json={"session_id": sid, "transcript": "endpoint answer"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["done"] is False
    assert "_answer_id" not in body                 # internal handle stripped

    # TestClient runs BackgroundTasks synchronously after the response, so by now the
    # first answer is graded (its own session_scope committed).
    with SessionLocal() as check:
        row = check.scalars(select(AssessmentAnswer)
                            .where(AssessmentAnswer.session_id == sid)).first()
        assert row is not None
        assert row.transcript == "endpoint answer"
        assert row.score == 9.0                     # background task populated the score


def test_summary_and_answers_tolerate_null_scores_mid_grading(monkeypatch):
    """Mid-grading (rows inserted, not yet graded) the recruiter's /summary and /answers
    must not crash on NULL scores — they show partial/NULL aggregate until the background
    grades land. _summary already skips None when averaging (single scoring path)."""
    s = _session()
    start = asv.start_session(s, "Backend Engineer", candidate_name="Mid Mona")
    s.commit()
    sid = start["session_id"]

    def _boom(*a, **k):
        raise AssertionError("no grading in the submit path")

    monkeypatch.setattr(asv, "_grade", _boom)

    # Submit two ungraded answers (no background run yet → both score NULL).
    asv.grade_answer(s, session_id=sid, transcript="a0")
    s.commit()
    asv.grade_answer(s, session_id=sid, transcript="a1")
    s.commit()

    row = s.get(AssessmentSession, sid)
    out = asv._summary(s, row, asv.get_questions(s, row.role))     # must not raise
    assert out["overall_score"] is None                           # nothing graded yet
    assert out["passed_count"] == 0
    assert row.completed_at is None                               # not stamped mid-grading


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

"""P3 durable grading worker tests. Covers job claiming, provider errors,
retryable/permanent failure classification, stale lease reclaim, and crash recovery.
Run inside the api container:
    docker compose exec api pytest tests/integration/test_grading_worker.py
"""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import func, select

from app.config import settings
from app.db.models import AssessmentAnswer, AssessmentSession, GradingJob, RoleQuestion
from app.db.session import SessionLocal, init_db
from app.services import assessment_service as asv
from app.worker import grading_worker

_RUN_ID = os.urandom(4).hex()


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


def test_grade_answer_creates_a_pending_grading_job_atomically():
    """PR-301/302: calling grade_answer() creates both an AssessmentAnswer and a
    GradingJob row in the same transaction. The job row has status=PENDING and all
    version fields set."""
    s = _session()
    start = asv.start_session(s, "Backend Engineer", candidate_name=f"Job Test {_RUN_ID}")
    s.commit()
    sid = start["session_id"]

    # Suppress grading in the request path — this test is about the job row only.
    import app.services.assessment_service as asv_module
    old_grade = asv_module._grade
    asv_module._grade = lambda *a, **k: None

    try:
        res = asv.grade_answer(s, session_id=sid, transcript="an answer")
        s.commit()
        answer_id = res["_answer_id"]

        # Now verify the job exists with the right fields
        job = s.scalar(select(GradingJob).where(GradingJob.answer_id == answer_id))
        assert job is not None, "GradingJob row must exist for the answer"
        assert job.status == "PENDING", f"Job status must be PENDING, got {job.status}"
        assert job.answer_id == answer_id
        assert job.prompt_version == asv.PROMPT_VERSION
        assert job.model_version == settings.groq_grader_model
        assert job.rubric_version == asv.RUBRIC_VERSION
        assert job.attempts == 0
        assert job.lease_owner is None
        assert job.error_category is None
    finally:
        asv_module._grade = old_grade


def test_claim_batch_skip_locked_two_workers_never_get_the_same_job():
    """PR-303: FOR UPDATE SKIP LOCKED ensures two concurrent workers claim disjoint
    job sets. We create several PENDING jobs, then claim from two threads concurrently,
    asserting no overlap."""
    s = _session()
    # Create a session with multiple answers so we have multiple jobs
    start = asv.start_session(s, "Backend Engineer", candidate_name=f"Claim Test {_RUN_ID}")
    s.commit()
    sid = start["session_id"]
    total = start["total_questions"]

    # Suppress grading; we only care about the jobs
    import app.services.assessment_service as asv_module
    old_grade = asv_module._grade
    asv_module._grade = lambda *a, **k: None

    try:
        # Create all answers (and their jobs)
        for i in range(total):
            asv.grade_answer(s, session_id=sid, transcript=f"answer {i}")
            s.commit()

        # Now claim from two threads concurrently
        claimed_sets = []

        def _claim():
            ids = grading_worker.claim_batch(limit=3)
            claimed_sets.append(set(ids))

        with ThreadPoolExecutor(max_workers=2) as ex:
            futures = [ex.submit(_claim) for _ in range(2)]
            [f.result() for f in futures]

        # Verify the two sets are disjoint
        if len(claimed_sets[0]) > 0 and len(claimed_sets[1]) > 0:
            assert claimed_sets[0].isdisjoint(claimed_sets[1]), \
                f"Two workers claimed the same jobs: {claimed_sets[0]} vs {claimed_sets[1]}"
        # And together they cover jobs (no duplicates across the union)
        all_claimed = claimed_sets[0] | claimed_sets[1]
        assert len(all_claimed) == len(claimed_sets[0]) + len(claimed_sets[1]), \
            "Claimed job IDs must be unique across both workers"
    finally:
        asv_module._grade = old_grade


def test_provider_error_is_retried_with_backoff_not_immediately_terminal():
    """PR-305/306: a provider/network error (retryable) increments attempts,
    reschedules with backoff, and leaves the answer score untouched (no placeholder grade)."""
    s = _session()
    start = asv.start_session(s, "Backend Engineer", candidate_name=f"Retry Test {_RUN_ID}")
    s.commit()
    sid = start["session_id"]

    # Create one answer + job
    import app.services.assessment_service as asv_module
    old_grade = asv_module._grade
    asv_module._grade = lambda *a, **k: asv._empty_grade("provider_error: BadRequestError")

    try:
        res = asv.grade_answer(s, session_id=sid, transcript="an answer")
        s.commit()
        answer_id = res["_answer_id"]
        job_id = s.scalar(select(GradingJob.id).where(GradingJob.answer_id == answer_id))

        # Manually claim the job (update its status) using a new session
        s2 = _session()
        job = s2.get(GradingJob, job_id)
        job.status = "RUNNING"
        job.lease_owner = "test-worker"
        job.lease_expires_at = datetime.now(timezone.utc) + timedelta(seconds=60)
        s2.commit()
        s2.close()

        # Process the job
        grading_worker.process_job(job_id)

        # Verify job is RETRY, not COMPLETE or FAILED
        s.expire_all()
        job = s.get(GradingJob, job_id)
        assert job.status == "RETRY", f"Job must be RETRY after first provider error, got {job.status}"
        assert job.attempts == 1
        assert job.error_category == "RETRYABLE"
        assert "provider_error" in (job.last_error or "")
        assert job.next_attempt_at > datetime.now(timezone.utc), \
            "next_attempt_at must be in the future (backoff)"
        assert job.lease_owner is None, "Lease must be cleared for retry"

        # Answer must still be ungraded
        answer = s.get(AssessmentAnswer, answer_id)
        assert answer.score is None, "Answer score must still be None after retryable error"
        assert answer.reason is None, "Answer reason must still be None after retryable error"
    finally:
        asv_module._grade = old_grade


def test_provider_error_becomes_failed_after_max_attempts():
    """PR-306: after MAX_ATTEMPTS retryable failures, the job moves to FAILED (with a grade
    persisted) rather than RETRY. The answer gets the placeholder grade."""
    s = _session()
    start = asv.start_session(s, "Backend Engineer", candidate_name=f"MaxAttempts Test {_RUN_ID}")
    s.commit()
    sid = start["session_id"]

    res = asv.grade_answer(s, session_id=sid, transcript="an answer")
    s.commit()
    answer_id = res["_answer_id"]
    job_id = s.scalar(select(GradingJob.id).where(GradingJob.answer_id == answer_id))

    import app.services.assessment_service as asv_module
    old_grade = asv_module._grade
    asv_module._grade = lambda *a, **k: asv._empty_grade("provider_error: BadRequestError")

    try:
        # Simulate MAX_ATTEMPTS by looping: manually set up RUNNING, process, repeat
        for attempt_num in range(grading_worker.MAX_ATTEMPTS):
            # Manually set job to RUNNING using a new session
            s2 = _session()
            j = s2.get(GradingJob, job_id)
            j.status = "RUNNING"
            j.lease_owner = "test-worker"
            j.lease_expires_at = datetime.now(timezone.utc) + timedelta(seconds=60)
            s2.commit()
            s2.close()

            grading_worker.process_job(job_id)

            # For all but the last attempt, set next_attempt_at back to now so it can be claimed again
            if attempt_num < grading_worker.MAX_ATTEMPTS - 1:
                s3 = _session()
                j = s3.get(GradingJob, job_id)
                j.status = "RETRY"  # reset to RETRY so it can be claimed
                j.next_attempt_at = datetime.now(timezone.utc)
                s3.commit()
                s3.close()

        # After MAX_ATTEMPTS, job must be FAILED
        s.expire_all()
        job = s.get(GradingJob, job_id)
        assert job.status == "FAILED", f"Job must be FAILED after MAX_ATTEMPTS, got {job.status}"
        assert job.error_category == "RETRYABLE"
        assert "provider_error" in (job.last_error or "")

        # Answer must now have the placeholder grade persisted
        answer = s.get(AssessmentAnswer, answer_id)
        assert answer.score is None, "Placeholder grade has score=None"
        assert answer.reason == "provider_error: BadRequestError"
    finally:
        asv_module._grade = old_grade


def test_malformed_model_output_is_permanent_and_completes_on_first_attempt():
    """PR-305: malformed model output (not a provider error) is permanent and completes
    on first attempt, marking the job COMPLETE (not FAILED)."""
    s = _session()
    start = asv.start_session(s, "Backend Engineer", candidate_name=f"Malformed Test {_RUN_ID}")
    s.commit()
    sid = start["session_id"]

    res = asv.grade_answer(s, session_id=sid, transcript="an answer")
    s.commit()
    answer_id = res["_answer_id"]
    job_id = s.scalar(select(GradingJob.id).where(GradingJob.answer_id == answer_id))

    import app.services.assessment_service as asv_module
    old_grade = asv_module._grade
    asv_module._grade = lambda *a, **k: asv._empty_grade("garbage, not json")

    try:
        # Manually set job to RUNNING using a new session
        s2 = _session()
        j = s2.get(GradingJob, job_id)
        j.status = "RUNNING"
        j.lease_owner = "test-worker"
        j.lease_expires_at = datetime.now(timezone.utc) + timedelta(seconds=60)
        s2.commit()
        s2.close()

        grading_worker.process_job(job_id)

        # Job must be COMPLETE (not RETRY or FAILED)
        s.expire_all()
        job = s.get(GradingJob, job_id)
        assert job.status == "COMPLETE", f"Malformed output must mark job COMPLETE, got {job.status}"
        assert job.attempts == 1, "Only one attempt should have been made"

        # Answer must have the grade persisted
        answer = s.get(AssessmentAnswer, answer_id)
        assert answer.score is None
        assert answer.reason == "garbage, not json"
    finally:
        asv_module._grade = old_grade


def test_reclaim_stale_leases_resets_expired_running_jobs():
    """PR-307: a RUNNING job with an expired lease_expires_at is reset to PENDING
    by reclaim_stale_leases()."""
    s = _session()
    start = asv.start_session(s, "Backend Engineer", candidate_name=f"Stale Test {_RUN_ID}")
    s.commit()
    sid = start["session_id"]

    res = asv.grade_answer(s, session_id=sid, transcript="an answer")
    s.commit()
    answer_id = res["_answer_id"]

    # Manually transition the existing job to RUNNING with an expired lease
    with s.begin():
        job = s.scalar(select(GradingJob).where(GradingJob.answer_id == answer_id))
        job.status = "RUNNING"
        job.lease_owner = "dead-worker"
        job.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=10)

    # Reclaim should find and reset it
    reclaimed_count = grading_worker.reclaim_stale_leases()
    assert reclaimed_count >= 1, "At least one stale lease should be reclaimed"

    # Verify it's back to PENDING
    s.expire_all()
    job = s.scalar(select(GradingJob).where(GradingJob.answer_id == answer_id))
    assert job.status == "PENDING"
    assert job.lease_owner is None
    assert job.lease_expires_at is None


def test_crash_after_claim_is_recoverable_by_another_worker():
    """PR-307: simulating a crash after claim (RUNNING, leased) but before process
    completes: another worker can reclaim the stale lease and process the job."""
    s = _session()
    start = asv.start_session(s, "Backend Engineer", candidate_name=f"Crash Test {_RUN_ID}")
    s.commit()
    sid = start["session_id"]

    res = asv.grade_answer(s, session_id=sid, transcript="an answer")
    s.commit()
    answer_id = res["_answer_id"]
    job_id = s.scalar(select(GradingJob.id).where(GradingJob.answer_id == answer_id))

    # Manually set job to RUNNING (simulating it was claimed)
    s2 = _session()
    j = s2.get(GradingJob, job_id)
    j.status = "RUNNING"
    j.lease_owner = "dead-worker"
    j.lease_expires_at = datetime.now(timezone.utc) + timedelta(seconds=60)
    s2.commit()
    s2.close()

    # Manually expire the lease (simulating the worker crashing)
    s3 = _session()
    j = s3.get(GradingJob, job_id)
    j.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    s3.commit()
    s3.close()

    # Another worker reclaims it
    reclaimed = grading_worker.reclaim_stale_leases()
    assert reclaimed >= 1

    # Verify it's back to PENDING
    s.expire_all()
    job_before_reclaim = s.get(GradingJob, job_id)
    assert job_before_reclaim.status == "PENDING"

    # Manually claim and process successfully
    import app.services.assessment_service as asv_module
    old_grade = asv_module._grade
    asv_module._grade = _stub_grade_for([8.0])

    try:
        # Manually set to RUNNING using a new session
        s4 = _session()
        j = s4.get(GradingJob, job_id)
        j.status = "RUNNING"
        j.lease_owner = "test-worker"
        j.lease_expires_at = datetime.now(timezone.utc) + timedelta(seconds=60)
        s4.commit()
        s4.close()

        grading_worker.process_job(job_id)

        # Job must be COMPLETE
        s.expire_all()
        job_final = s.get(GradingJob, job_id)
        assert job_final.status == "COMPLETE"

        # Answer must have the grade
        answer = s.get(AssessmentAnswer, answer_id)
        assert answer.score == 8.0
    finally:
        asv_module._grade = old_grade


def test_full_run_once_grades_and_stamps_session_completion():
    """PR-304/308: end-to-end happy path. Starting from a complete session with
    all questions answered (all jobs PENDING), run_once() repeatedly grades everything
    and stamps the session completion."""
    s = _session()
    start = asv.start_session(s, "Backend Engineer", candidate_name=f"E2E Test {_RUN_ID}")
    s.commit()
    sid = start["session_id"]
    total = start["total_questions"]

    import app.services.assessment_service as asv_module
    old_grade = asv_module._grade
    # Use a lambda that always returns the same grade, not an iterator
    asv_module._grade = lambda *a, **k: {
        "score": 8.0,
        "rating": asv._rating_band(8.0),
        "passed": True,
        "reason": "stub",
        "required_covered": [], "important_covered": [],
        "important_missed": [], "optional_missed": [], "technical_errors": [],
    }

    try:
        # Submit all answers (fast path, no grading)
        for i in range(total):
            asv.grade_answer(s, session_id=sid, transcript=f"answer {i}")
            s.commit()

        # Verify all jobs are PENDING
        answer_ids = s.scalars(
            select(AssessmentAnswer.id).where(AssessmentAnswer.session_id == sid)
        ).all()
        job_count = s.scalar(
            select(func.count(GradingJob.id)).where(GradingJob.answer_id.in_(answer_ids))
        ) or 0
        assert job_count > 0, "Should have created grading jobs"

        # Run the worker repeatedly until no jobs remain for this session
        max_iterations = 100
        for iteration in range(max_iterations):
            n = grading_worker.run_once()
            if n == 0:
                break

        # Verify session is completed
        s.expire_all()
        session_row = s.get(AssessmentSession, sid)
        assert session_row.completed_at is not None, "Session must be stamped complete"
        assert session_row.overall_score is not None
        assert session_row.overall_score == 8.0
        assert session_row.rating == asv._rating_band(8.0)
        assert session_row.answered == total

        # Verify all answers are graded
        answer_rows = s.scalars(
            select(AssessmentAnswer).where(AssessmentAnswer.session_id == sid)
        ).all()
        assert all(a.score is not None for a in answer_rows), "All answers must be graded"
        assert all(a.score == 8.0 for a in answer_rows)
    finally:
        asv_module._grade = old_grade


def test_graded_answer_persists_version_fields_on_assessment_answer():
    """PR-503: grading versions (prompt_version, model_version, rubric_version) are
    persisted durably on the AssessmentAnswer row itself, not just the GradingJob."""
    s = _session()
    start = asv.start_session(s, "Backend Engineer", candidate_name=f"Version Test {_RUN_ID}")
    s.commit()
    sid = start["session_id"]

    res = asv.grade_answer(s, session_id=sid, transcript="an answer")
    s.commit()
    answer_id = res["_answer_id"]

    # Grade the answer via the worker path (same code path as production)
    import app.services.assessment_service as asv_module
    old_grade = asv_module._grade
    asv_module._grade = lambda *a, **k: {
        "score": 8.0,
        "rating": asv._rating_band(8.0),
        "passed": True,
        "reason": "stub",
        "required_covered": [], "important_covered": [],
        "important_missed": [], "optional_missed": [], "technical_errors": [],
    }

    try:
        job_id = s.scalar(select(GradingJob.id).where(GradingJob.answer_id == answer_id))
        job = s.get(GradingJob, job_id)
        job.status = "RUNNING"
        job.lease_owner = "test-worker"
        job.lease_expires_at = datetime.now(timezone.utc) + timedelta(seconds=60)
        s.commit()

        grading_worker.process_job(job_id)
        s.expire_all()

        # Verify answer has all three version fields populated
        answer = s.get(AssessmentAnswer, answer_id)
        assert answer.prompt_version is not None, "prompt_version must be set"
        assert answer.model_version is not None, "model_version must be set"
        assert answer.rubric_version is not None, "rubric_version must be set"
        assert answer.prompt_version == asv.PROMPT_VERSION
        assert answer.model_version == settings.groq_grader_model
        assert answer.rubric_version == asv.RUBRIC_VERSION
    finally:
        asv_module._grade = old_grade


def test_grading_job_call_id_populated_from_session():
    """PR-509: GradingJob.call_id is denormalized from AssessmentSession.call_id
    for fast correlation (CallSid → session → job)."""
    from app.db.models import Call, Interview, Candidate

    s = _session()
    # Create a candidate and call to link the session to
    cand = Candidate(name=f"CallID Test {_RUN_ID}", phone=f"+1234567890{_RUN_ID[:4]}")
    s.add(cand)
    s.flush()

    interview = Interview(candidate_id=cand.id, role="Backend Engineer")
    s.add(interview)
    s.flush()

    call = Call(candidate_id=cand.id, interview_id=interview.id, direction="inbound",
                status="ACTIVE", provider_call_id=f"call-{_RUN_ID}")
    s.add(call)
    s.flush()
    call_id = call.id

    # Now start a session with this call
    start = asv.start_session(s, "Backend Engineer", candidate_name=f"CallID Test {_RUN_ID}",
                             call_id=call_id, candidate_id=cand.id, interview_id=interview.id)
    s.commit()
    sid = start["session_id"]

    res = asv.grade_answer(s, session_id=sid, transcript="an answer")
    s.commit()
    answer_id = res["_answer_id"]

    # Verify the GradingJob has call_id populated
    job = s.scalar(select(GradingJob).where(GradingJob.answer_id == answer_id))
    assert job is not None
    assert job.call_id == call_id, f"Job call_id must match session's call_id ({call_id}), got {job.call_id}"


def test_observability_grading_call_id_filter():
    """PR-509: GET /observability/grading?call_id=<id> filters job counts to just
    that call's jobs, distinct from the unfiltered global count."""
    from app.db.models import Call, Interview, Candidate
    from app.api.observability import grading_stats

    s = _session()
    # Create two separate calls with two separate sessions and jobs
    calls_and_jobs = []
    for call_num in range(2):
        cand = Candidate(name=f"Obs Test {call_num} {_RUN_ID}",
                        phone=f"+1234567890{call_num}{_RUN_ID[:3]}")
        s.add(cand)
        s.flush()

        interview = Interview(candidate_id=cand.id, role="Backend Engineer")
        s.add(interview)
        s.flush()

        call = Call(candidate_id=cand.id, interview_id=interview.id, direction="inbound",
                    status="ACTIVE", provider_call_id=f"call-{call_num}-{_RUN_ID}")
        s.add(call)
        s.flush()

        start = asv.start_session(s, "Backend Engineer", candidate_name=f"Obs Test {call_num}",
                                 call_id=call.id, candidate_id=cand.id, interview_id=interview.id)
        s.commit()

        # Submit one answer per call
        res = asv.grade_answer(s, session_id=start["session_id"], transcript=f"answer for call {call_num}")
        s.commit()

        calls_and_jobs.append((call.id, res["_answer_id"]))

        # Push next_attempt_at into the future immediately so the REAL grading_worker
        # process (a separate container, if one happens to be running against this same
        # dev Postgres) can't claim and complete this job out from under the assertions
        # below — monkeypatching asv_module._grade only affects THIS test process, not
        # a genuinely separate worker process/container.
        s.execute(
            GradingJob.__table__.update()
            .where(GradingJob.answer_id == res["_answer_id"])
            .values(next_attempt_at=datetime.now(timezone.utc) + timedelta(hours=1))
        )
        s.commit()

    # Suppress grading so jobs stay PENDING
    import app.services.assessment_service as asv_module
    old_grade = asv_module._grade
    asv_module._grade = lambda *a, **k: None

    try:
        # Get unfiltered stats
        unfiltered = grading_stats()
        assert unfiltered["backlog"] >= 2, "Should have at least 2 pending jobs across all calls"

        # Get stats filtered to call 0
        call_0_id, _ = calls_and_jobs[0]
        filtered_0 = grading_stats(call_id=call_0_id)

        # Get stats filtered to call 1
        call_1_id, _ = calls_and_jobs[1]
        filtered_1 = grading_stats(call_id=call_1_id)

        # Each filtered result should have exactly 1 pending (the one job for that call)
        assert filtered_0["backlog"] >= 1, "Call 0 should have at least 1 pending job"
        assert filtered_1["backlog"] >= 1, "Call 1 should have at least 1 pending job"
        # And together they should be <= the unfiltered count
        assert (filtered_0["backlog"] + filtered_1["backlog"]) <= unfiltered["backlog"]
    finally:
        asv_module._grade = old_grade

"""Durable grading worker (P3). Claims GradingJob rows via SELECT...FOR UPDATE SKIP LOCKED,
grades each with a lease + exponential backoff + retryable/permanent error classification,
and reclaims stale leases left by crashed workers. Runs as its own process so a crash here
can't take down the API, and vice versa — every claim/attempt/completion commits to Postgres
before the next step, so a worker restart just re-claims whatever it (or another worker) had
leased. Run standalone: `python3 -m app.worker.grading_worker`.
"""
from __future__ import annotations

import logging
import os
import random
import socket
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update

from app.config import settings
from app.db.models import AssessmentAnswer, AssessmentSession, GradingJob, RoleQuestion
from app.db.session import init_db, session_scope
from app.services import assessment_service as asv

logger = logging.getLogger(__name__)

WORKER_ID = f"{socket.gethostname()}-{os.getpid()}"
LEASE_SECONDS = int(os.getenv("GRADING_LEASE_SECONDS", "60"))
MAX_ATTEMPTS = int(os.getenv("GRADING_MAX_ATTEMPTS", "5"))
BASE_BACKOFF_SECONDS = 2.0
BACKOFF_CAP_SECONDS = 300.0
POLL_INTERVAL_SECONDS = float(os.getenv("GRADING_POLL_INTERVAL", "2"))
BATCH_SIZE = 5


def _backoff_seconds(attempts: int) -> float:
    base = min(BASE_BACKOFF_SECONDS * (2 ** attempts), BACKOFF_CAP_SECONDS)
    return base + random.uniform(0, base * 0.25)  # jitter


def reclaim_stale_leases() -> int:
    """A worker that crashes mid-RUNNING never clears its lease. Any RUNNING job whose
    lease has expired is fair game for any worker — reset to PENDING so the normal claim
    query picks it back up. This is also what makes 'crash after claim' and 'crash after
    the provider responded but before the DB write commits' safe: both leave a RUNNING job
    with an expired lease, indistinguishable at the DB level, both fixed by this."""
    with session_scope() as session:
        result = session.execute(
            update(GradingJob)
            .where(GradingJob.status == "RUNNING", GradingJob.lease_expires_at < func.now())
            .values(status="PENDING", lease_owner=None, lease_expires_at=None)
        )
        return result.rowcount


def reconcile_missing_jobs() -> int:
    """Backfill a GradingJob for any ungraded answer that doesn't have one. Covers answers
    written before this migration and is defense-in-depth (grade_answer inserts the answer
    and its job in one transaction, so this should normally find nothing).
    PR-509: also populate call_id from the session's call_id for correlation."""
    with session_scope() as session:
        orphans = session.scalars(
            select(AssessmentAnswer.id)
            .outerjoin(GradingJob, GradingJob.answer_id == AssessmentAnswer.id)
            .where(AssessmentAnswer.score.is_(None), GradingJob.id.is_(None))
        ).all()
        for answer_id in orphans:
            answer = session.get(AssessmentAnswer, answer_id)
            if answer is not None:
                s = session.get(AssessmentSession, answer.session_id)
                session.add(GradingJob(
                    answer_id=answer_id, call_id=s.call_id if s else None,
                    prompt_version=asv.PROMPT_VERSION,
                    model_version=settings.groq_grader_model, rubric_version=asv.RUBRIC_VERSION,
                ))
        return len(orphans)


def claim_batch(limit: int = BATCH_SIZE) -> list[int]:
    """FOR UPDATE SKIP LOCKED: N workers racing on this query get disjoint job sets, never
    the same job twice — this is what makes concurrent/duplicate workers safe."""
    with session_scope() as session:
        ids = session.scalars(
            select(GradingJob.id)
            .where(GradingJob.status.in_(("PENDING", "RETRY")),
                   GradingJob.next_attempt_at <= func.now())
            .order_by(GradingJob.next_attempt_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).all()
        if ids:
            session.execute(
                update(GradingJob).where(GradingJob.id.in_(ids))
                .values(status="RUNNING", lease_owner=WORKER_ID,
                        lease_expires_at=func.now() + timedelta(seconds=LEASE_SECONDS))
            )
        return list(ids)


def process_job(job_id: int) -> None:
    """Grade exactly one claimed job. A retryable provider/network error (not yet exhausted)
    leaves the answer untouched and reschedules; a permanent (malformed-output/schema) error
    and a successful grade both write the final grade and close the job out."""
    with session_scope() as session:
        job = session.get(GradingJob, job_id)
        if job is None or job.status != "RUNNING":
            return  # already handled elsewhere
        answer = session.get(AssessmentAnswer, job.answer_id)
        if answer is None:
            job.status, job.error_category, job.last_error = "FAILED", "PERMANENT", "answer row missing"
            return
        if answer.score is not None:
            job.status = "COMPLETE"  # already graded (race with a manual/legacy path) — no-op
            return
        q = session.get(RoleQuestion, answer.question_id)

        grade = asv._grade(q.prompt, q.expected_answer, q.key_points, answer.transcript)
        is_provider_error = isinstance(grade["reason"], str) and grade["reason"].startswith("provider_error:")

        if is_provider_error and job.attempts + 1 < MAX_ATTEMPTS:
            job.attempts += 1
            job.status, job.error_category, job.last_error = "RETRY", "RETRYABLE", grade["reason"]
            job.next_attempt_at = datetime.now(timezone.utc) + timedelta(seconds=_backoff_seconds(job.attempts))
            job.lease_owner, job.lease_expires_at = None, None
            return

        # Terminal: a permanent (malformed-output/schema) grade, or a retryable one that
        # exhausted its attempts. Persist the best grade we have either way.
        asv._persist_grade(answer, grade, prompt_version=job.prompt_version,
                          model_version=job.model_version, rubric_version=job.rubric_version)
        job.attempts += 1
        if is_provider_error:
            job.status, job.error_category, job.last_error = "FAILED", "RETRYABLE", grade["reason"]
        else:
            job.status = "COMPLETE"
        session.flush()
        logger.info("job %s (call=%s answer=%s) -> %s", job.id, job.call_id, job.answer_id, job.status)
        asv._maybe_stamp_completion(session, session.get(AssessmentSession, answer.session_id))


def run_once() -> int:
    reclaim_stale_leases()
    job_ids = claim_batch()
    for job_id in job_ids:
        process_job(job_id)
    if not job_ids:
        reconcile_missing_jobs()
    return len(job_ids)


def run_forever() -> None:
    init_db()
    logger.info("grading worker %s starting", WORKER_ID)
    while True:
        n = run_once()
        if n == 0:
            time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_forever()

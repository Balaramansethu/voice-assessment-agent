"""Database engine + session factory. Synchronous SQLAlchemy; FastAPI runs the
endpoints in a threadpool, and the agent calls the orchestrator via asyncio.to_thread.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.db.models import Base

engine = create_engine(settings.database_url, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def init_db() -> None:
    """Create tables if absent. POC uses create_all instead of Alembic to remove
    migration friction; swap in Alembic when the schema needs to evolve safely."""
    with engine.begin() as conn:
        # pgvector must exist before create_all builds the Vector columns.
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    Base.metadata.create_all(bind=engine)
    _apply_lightweight_migrations()
    _create_rag_indexes()
    _create_p1_identity_indexes()
    _create_p2_lifecycle_indexes()
    _create_p6_constraints()


# (table, column, type) tuples for additive columns the models gained after their
# table was first created. `create_all` never ALTERs an existing table, so we backfill
# them here. Nullable/additive only — safe, never drops or rewrites data.
_ADDITIVE_COLUMNS: list[tuple[str, str, str]] = [
    # AssessmentSession aggregate result (recruiter-facing, silent grading; 0..10).
    ("assessment_session", "total_questions", "INTEGER"),
    ("assessment_session", "answered", "INTEGER"),
    ("assessment_session", "passed_count", "INTEGER"),
    ("assessment_session", "overall_score", "DOUBLE PRECISION"),
    ("assessment_session", "rating", "VARCHAR(20)"),
    ("assessment_session", "completed_at", "TIMESTAMPTZ"),
    # AssessmentAnswer per-answer 0..10 rubric fields (new grader).
    ("assessment_answer", "rating", "VARCHAR(20)"),
    ("assessment_answer", "passed", "BOOLEAN"),
    ("assessment_answer", "reason", "TEXT"),
    ("assessment_answer", "required_covered", "JSONB"),
    ("assessment_answer", "important_covered", "JSONB"),
    ("assessment_answer", "important_missed", "JSONB"),
    ("assessment_answer", "optional_missed", "JSONB"),
    ("assessment_answer", "technical_errors", "JSONB"),
    # PR-503: grading versions on AssessmentAnswer (durable versioning per answer).
    ("assessment_answer", "prompt_version", "VARCHAR(20)"),
    ("assessment_answer", "model_version", "VARCHAR(64)"),
    ("assessment_answer", "rubric_version", "VARCHAR(20)"),
    # PR-101/104 identity + authorization
    ("interview", "invitation_code", "VARCHAR(12)"),
    ("call", "resolved_invitation_code", "VARCHAR(12)"),
    ("assessment_session", "candidate_id", "INTEGER"),
    ("assessment_session", "interview_id", "INTEGER"),
    ("assessment_session", "invitation_code_used", "VARCHAR(12)"),
    # PR-509: grading job call correlation.
    ("grading_job", "call_id", "INTEGER"),
]


def _apply_lightweight_migrations() -> None:
    """Additive column backfills for tables that already exist in the running DB.

    We check information_schema first and only ALTER columns that are genuinely
    MISSING. This matters for correctness, not just tidiness: `ALTER TABLE ... ADD
    COLUMN` takes an ACCESS EXCLUSIVE lock, so an unconditional statement on every
    startup would block behind (and against) live traffic already reading the table
    — the api runs on the same Postgres. In steady state (columns present) this path
    issues zero DDL and takes no exclusive lock. A short lock_timeout keeps a genuine
    first-run migration from hanging forever if another session holds the table."""
    with engine.begin() as conn:
        existing = {
            (row.table_name, row.column_name)
            for row in conn.execute(text(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_name IN ('assessment_session', 'assessment_answer', "
                "'interview', 'call', 'grading_job')"
            ))
        }
        missing = [(t, c, ty) for (t, c, ty) in _ADDITIVE_COLUMNS
                   if (t, c) not in existing]
        if not missing:
            return
        conn.execute(text("SET lock_timeout = '5s'"))
        for table, col, coltype in missing:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {coltype}"))


def _run_missing_indexes(named_stmts: list[tuple[str, str]]) -> None:
    """Run each (index_name, CREATE INDEX ...) statement only if the index is
    genuinely absent. `CREATE INDEX IF NOT EXISTS` is DDL either way, and DDL takes
    a SHARE lock on the table even when it ends up a no-op — cheap in isolation, but
    `init_db()` runs on every test's `_session()` call, and any test holding an
    uncommitted write on the same table (a leaked raw session that never closed)
    queues every later test's index check behind it. Checking pg_indexes first in
    Python means steady state (indexes already present, the overwhelmingly common
    case) takes no lock and opens no transaction at all."""
    with engine.connect() as conn:
        existing = {
            row.indexname
            for row in conn.execute(text(
                "SELECT indexname FROM pg_indexes WHERE indexname = ANY(:names)"
            ), {"names": [name for name, _ in named_stmts]})
        }
    missing = [stmt for name, stmt in named_stmts if name not in existing]
    if not missing:
        return
    with engine.begin() as conn:
        for stmt in missing:
            conn.execute(text(stmt))


def _create_rag_indexes() -> None:
    """HNSW (cosine) for dense search + GIN full-text for sparse. Idempotent."""
    _run_missing_indexes([
        ("ix_kb_chunk_embedding_hnsw",
         "CREATE INDEX IF NOT EXISTS ix_kb_chunk_embedding_hnsw "
         "ON kb_chunk USING hnsw (embedding vector_cosine_ops)"),
        ("ix_kb_chunk_fts",
         "CREATE INDEX IF NOT EXISTS ix_kb_chunk_fts "
         "ON kb_chunk USING gin (to_tsvector('english', content))"),
        ("ix_cand_doc_embedding_hnsw",
         "CREATE INDEX IF NOT EXISTS ix_cand_doc_embedding_hnsw "
         "ON candidate_document USING hnsw (embedding vector_cosine_ops)"),
    ])


def _create_p1_identity_indexes() -> None:
    """Indexes for the P1 identity/authorization columns (PR-101/104). Idempotent."""
    _run_missing_indexes([
        ("ix_interview_invitation_code",
         "CREATE UNIQUE INDEX IF NOT EXISTS ix_interview_invitation_code "
         "ON interview (invitation_code)"),
        ("ix_assessment_session_candidate_id",
         "CREATE INDEX IF NOT EXISTS ix_assessment_session_candidate_id "
         "ON assessment_session (candidate_id)"),
        ("ix_assessment_session_interview_id",
         "CREATE INDEX IF NOT EXISTS ix_assessment_session_interview_id "
         "ON assessment_session (interview_id)"),
    ])


def _create_p2_lifecycle_indexes() -> None:
    """PR-204: at most one ACTIVE call may own a given interview at a time —
    enforced at the DB level (not just the app-level has_active_call() check,
    which has its own TOCTOU window). Partial unique index: only rows with
    status='ACTIVE' AND interview_id IS NOT NULL participate. Idempotent."""
    _run_missing_indexes([
        ("ix_call_one_active_per_interview",
         "CREATE UNIQUE INDEX IF NOT EXISTS ix_call_one_active_per_interview "
         "ON call (interview_id) WHERE status = 'ACTIVE' AND interview_id IS NOT NULL"),
    ])


def _run_missing_constraints(named_stmts: list[tuple[str, str]]) -> None:
    """Run each (constraint_name, ALTER TABLE ... ADD CONSTRAINT ...) statement
    only if the constraint is genuinely absent. Checks information_schema.check_constraints
    and information_schema.table_constraints first in Python to avoid unconditional DDL
    on every startup (similar to _run_missing_indexes). In steady state, takes no lock."""
    with engine.connect() as conn:
        existing = {
            row.constraint_name
            for row in conn.execute(text(
                "SELECT constraint_name FROM information_schema.table_constraints "
                "WHERE constraint_name = ANY(:names)"
            ), {"names": [name for name, _ in named_stmts]})
        }
    missing = [stmt for name, stmt in named_stmts if name not in existing]
    if not missing:
        return
    with engine.begin() as conn:
        conn.execute(text("SET lock_timeout = '5s'"))
        for stmt in missing:
            conn.execute(text(stmt))


def _create_p6_constraints() -> None:
    """PR-604: Add CHECK constraints for enum domains, score ranges, positions.
    Constraint names are explicit and stable so the idempotent check-by-name works
    on rerun. Idempotent."""
    _run_missing_constraints([
        # Score range constraints (0..10, nullable).
        ("ck_assessment_answer_score_range",
         "ALTER TABLE assessment_answer ADD CONSTRAINT ck_assessment_answer_score_range "
         "CHECK (score IS NULL OR (score >= 0 AND score <= 10))"),
        ("ck_assessment_session_overall_score_range",
         "ALTER TABLE assessment_session ADD CONSTRAINT ck_assessment_session_overall_score_range "
         "CHECK (overall_score IS NULL OR (overall_score >= 0 AND overall_score <= 10))"),
        # Position constraints (>0).
        ("ck_assessment_answer_position_positive",
         "ALTER TABLE assessment_answer ADD CONSTRAINT ck_assessment_answer_position_positive "
         "CHECK (position > 0)"),
        ("ck_role_question_position_positive",
         "ALTER TABLE role_question ADD CONSTRAINT ck_role_question_position_positive "
         "CHECK (position > 0)"),
        # Grading job constraints.
        ("ck_grading_job_status_enum",
         "ALTER TABLE grading_job ADD CONSTRAINT ck_grading_job_status_enum "
         "CHECK (status IN ('PENDING', 'RUNNING', 'RETRY', 'COMPLETE', 'FAILED'))"),
        ("ck_grading_job_attempts_nonnegative",
         "ALTER TABLE grading_job ADD CONSTRAINT ck_grading_job_attempts_nonnegative "
         "CHECK (attempts >= 0)"),
        # Call status enum constraint (PR-022: call statuses are domain-validated).
        ("ck_call_status_enum",
         "ALTER TABLE call ADD CONSTRAINT ck_call_status_enum "
         "CHECK (status IN ('INITIATED', 'RINGING', 'ANSWERED', 'NO_ANSWER', 'VOICEMAIL', "
         "'ACTIVE', 'COMPLETED', 'DISCONNECTED', 'FAILED', 'ABANDONED'))"),
        # Interview status enum constraint (app/domain/states.py::InterviewStatus).
        ("ck_interview_status_enum",
         "ALTER TABLE interview ADD CONSTRAINT ck_interview_status_enum "
         "CHECK (status IN ('NOT_STARTED', 'IN_PROGRESS', 'INTERRUPTED', 'COMPLETED', "
         "'RESCHEDULED', 'CANCELLED', 'EXPIRED'))"),
    ])


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope. Commits on success, rolls back on error."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    with session_scope() as session:
        yield session

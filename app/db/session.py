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
                "WHERE table_name IN ('assessment_session', 'assessment_answer')"
            ))
        }
        missing = [(t, c, ty) for (t, c, ty) in _ADDITIVE_COLUMNS
                   if (t, c) not in existing]
        if not missing:
            return
        conn.execute(text("SET lock_timeout = '5s'"))
        for table, col, coltype in missing:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {coltype}"))


def _create_rag_indexes() -> None:
    """HNSW (cosine) for dense search + GIN full-text for sparse. Idempotent."""
    stmts = [
        "CREATE INDEX IF NOT EXISTS ix_kb_chunk_embedding_hnsw "
        "ON kb_chunk USING hnsw (embedding vector_cosine_ops)",
        "CREATE INDEX IF NOT EXISTS ix_kb_chunk_fts "
        "ON kb_chunk USING gin (to_tsvector('english', content))",
        "CREATE INDEX IF NOT EXISTS ix_cand_doc_embedding_hnsw "
        "ON candidate_document USING hnsw (embedding vector_cosine_ops)",
    ]
    with engine.begin() as conn:
        for s in stmts:
            conn.execute(text(s))


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

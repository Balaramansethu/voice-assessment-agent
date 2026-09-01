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


def _apply_lightweight_migrations() -> None:
    """Additive column backfills for tables that already exist in the running DB.

    `create_all` only CREATEs missing tables — it never ALTERs an existing one, so
    columns added to a model after its table was first created won't appear. Until
    Alembic lands, we apply idempotent `ADD COLUMN IF NOT EXISTS` here. Additive and
    nullable only (safe to run on every startup); never drops or rewrites data."""
    stmts = [
        # AssessmentSession aggregate result (recruiter-facing, silent grading).
        "ALTER TABLE assessment_session ADD COLUMN IF NOT EXISTS total_questions INTEGER",
        "ALTER TABLE assessment_session ADD COLUMN IF NOT EXISTS answered INTEGER",
        "ALTER TABLE assessment_session ADD COLUMN IF NOT EXISTS correct_count INTEGER",
        "ALTER TABLE assessment_session ADD COLUMN IF NOT EXISTS average_score DOUBLE PRECISION",
        "ALTER TABLE assessment_session ADD COLUMN IF NOT EXISTS rating VARCHAR(20)",
        "ALTER TABLE assessment_session ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ",
    ]
    with engine.begin() as conn:
        for s in stmts:
            conn.execute(text(s))


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

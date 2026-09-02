"""SQLAlchemy models — PostgreSQL is the single source of truth."""
from __future__ import annotations

from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.config import settings

EMBED_DIM = settings.embedding_dim


class Base(DeclarativeBase):
    pass


def _now() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now())


class Candidate(Base):
    __tablename__ = "candidate"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    phone: Mapped[str | None] = mapped_column(String(32), index=True, unique=True)
    email: Mapped[str | None] = mapped_column(String(200), index=True)
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    interviews: Mapped[list["Interview"]] = relationship(back_populates="candidate")


class Interview(Base):
    __tablename__ = "interview"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("candidate.id"), index=True)
    role: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(32), default="NOT_STARTED", index=True)
    current_question: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    candidate: Mapped["Candidate"] = relationship(back_populates="interviews")
    questions: Mapped[list["InterviewQuestion"]] = relationship(
        back_populates="interview", order_by="InterviewQuestion.position"
    )
    answers: Mapped[list["InterviewAnswer"]] = relationship(back_populates="interview")


class InterviewQuestion(Base):
    __tablename__ = "interview_question"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    interview_id: Mapped[int] = mapped_column(ForeignKey("interview.id"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    prompt_text: Mapped[str] = mapped_column(Text)
    expected_kind: Mapped[str] = mapped_column(String(50), default="open")
    created_at: Mapped[datetime] = _now()

    interview: Mapped["Interview"] = relationship(back_populates="questions")

    __table_args__ = (UniqueConstraint("interview_id", "position"),)


class InterviewAnswer(Base):
    __tablename__ = "interview_answer"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    interview_id: Mapped[int] = mapped_column(ForeignKey("interview.id"), index=True)
    question_id: Mapped[int] = mapped_column(ForeignKey("interview_question.id"))
    transcript: Mapped[str] = mapped_column(Text, default="")
    asr_confidence: Mapped[float | None] = mapped_column()
    call_id: Mapped[int | None] = mapped_column(ForeignKey("call.id"))
    recorded_at: Mapped[datetime] = _now()

    interview: Mapped["Interview"] = relationship(back_populates="answers")

    # one answer per question — enforces "resume = next unanswered question"
    __table_args__ = (UniqueConstraint("interview_id", "question_id"),)


class Call(Base):
    __tablename__ = "call"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    candidate_id: Mapped[int | None] = mapped_column(ForeignKey("candidate.id"), index=True)
    interview_id: Mapped[int | None] = mapped_column(ForeignKey("interview.id"), index=True)
    direction: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(32), index=True)
    # Idempotency anchor: Twilio CallSid or WebRTC session id. Unique so retried
    # webhooks / double taps collapse to one row.
    provider_call_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    transport: Mapped[str] = mapped_column(String(16), default="webrtc")
    from_number: Mapped[str | None] = mapped_column(String(32))
    to_number: Mapped[str | None] = mapped_column(String(32))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now()


class InterviewEvent(Base):
    """Lightweight audit trail — replaces any need for Kafka in the POC."""
    __tablename__ = "interview_event"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    interview_id: Mapped[int | None] = mapped_column(ForeignKey("interview.id"), index=True)
    call_id: Mapped[int | None] = mapped_column(ForeignKey("call.id"))
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _now()


# ─────────────────────────── RAG layer ───────────────────────────
# Knowledge base is stored in the same Postgres (single source of truth). Vectors
# live in pgvector columns; sparse retrieval uses Postgres full-text (tsvector).


class KBSource(Base):
    """A knowledge-base document (company/role/faq/rubric)."""
    __tablename__ = "kb_source"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    type: Mapped[str] = mapped_column(String(32), index=True)          # company|role|faq|rubric
    title: Mapped[str] = mapped_column(String(300))
    uri: Mapped[str | None] = mapped_column(String(500))
    role: Mapped[str | None] = mapped_column(String(200), index=True)  # scope, e.g. "Backend Engineer"
    visibility: Mapped[str] = mapped_column(String(16), default="candidate")  # candidate|internal
    version: Mapped[int] = mapped_column(Integer, default=1)
    checksum: Mapped[str | None] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = _now()

    chunks: Mapped[list["KBChunk"]] = relationship(
        back_populates="source", cascade="all, delete-orphan"
    )


class KBChunk(Base):
    __tablename__ = "kb_chunk"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("kb_source.id", ondelete="CASCADE"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    heading: Mapped[str | None] = mapped_column(String(300))
    token_count: Mapped[int] = mapped_column(Integer, default=0)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBED_DIM))
    meta: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _now()

    source: Mapped["KBSource"] = relationship(back_populates="chunks")

    __table_args__ = (UniqueConstraint("source_id", "ordinal"),)


class CandidateDocument(Base):
    """Candidate-scoped documents (resume/application/prior feedback). Retrieval is
    always row-filtered by candidate_id — a caller can only reach their own."""
    __tablename__ = "candidate_document"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("candidate.id"), index=True)
    doc_type: Mapped[str] = mapped_column(String(32))                 # resume|application|prior_feedback
    visibility: Mapped[str] = mapped_column(String(16), default="candidate")  # candidate|internal
    content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBED_DIM))
    meta: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _now()


class AnswerEvaluation(Base):
    """Rubric-grounded score for a candidate answer. Written via the orchestrator."""
    __tablename__ = "answer_evaluation"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    interview_id: Mapped[int] = mapped_column(ForeignKey("interview.id"), index=True)
    question_id: Mapped[int] = mapped_column(ForeignKey("interview_question.id"))
    answer_id: Mapped[int | None] = mapped_column(ForeignKey("interview_answer.id"))
    rubric_source_id: Mapped[int | None] = mapped_column(ForeignKey("kb_source.id"))
    score: Mapped[float | None] = mapped_column(Float)
    rationale: Mapped[str | None] = mapped_column(Text)
    retrieved_context: Mapped[dict | None] = mapped_column(JSONB)
    model: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = _now()


class RoleQuestion(Base):
    """Pre-seeded assessment question for a role, with its own 'correct answer'
    definition (expected answer + key points) used for live grading."""
    __tablename__ = "role_question"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    role: Mapped[str] = mapped_column(String(200), index=True)
    position: Mapped[int] = mapped_column(Integer)
    prompt: Mapped[str] = mapped_column(Text)
    expected_answer: Mapped[str] = mapped_column(Text)
    key_points: Mapped[list | None] = mapped_column(JSONB)
    difficulty: Mapped[str] = mapped_column(String(20), default="medium")
    created_at: Mapped[datetime] = _now()

    __table_args__ = (UniqueConstraint("role", "position"),)


class AssessmentSession(Base):
    """A lightweight quiz run for one role. No START/RESUME/COMPLETE lifecycle.

    The final result (aggregate rating) is derived from the persisted
    `assessment_answer` verdicts/scores and stamped here on completion — the
    recruiter-facing record. Never spoken to the candidate (silent grading)."""
    __tablename__ = "assessment_session"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    role: Mapped[str] = mapped_column(String(200), index=True)
    candidate_name: Mapped[str | None] = mapped_column(String(200))
    call_id: Mapped[int | None] = mapped_column(ForeignKey("call.id"))
    # Final aggregate result, written once when the last question is graded.
    # NULL until completion; derived from assessment_answer (single source of truth).
    total_questions: Mapped[int | None] = mapped_column(Integer)
    answered: Mapped[int | None] = mapped_column(Integer)
    passed_count: Mapped[int | None] = mapped_column(Integer)     # answers scoring >= 7/10
    overall_score: Mapped[float | None] = mapped_column(Float)    # 0..10
    rating: Mapped[str | None] = mapped_column(String(20))        # Excellent..Incorrect band
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now()


class AssessmentAnswer(Base):
    """A graded answer within an assessment session (graded silently, not spoken)."""
    __tablename__ = "assessment_answer"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("assessment_session.id"), index=True)
    question_id: Mapped[int] = mapped_column(ForeignKey("role_question.id"))
    position: Mapped[int] = mapped_column(Integer)
    transcript: Mapped[str] = mapped_column(Text)
    score: Mapped[float | None] = mapped_column(Float)        # 0..10 (weighted rubric)
    rating: Mapped[str | None] = mapped_column(String(20))    # Excellent | Strong | Good | Partial | Weak | Incorrect
    passed: Mapped[bool | None] = mapped_column(Boolean)      # score >= 7.0
    reason: Mapped[str | None] = mapped_column(Text)
    required_covered: Mapped[list | None] = mapped_column(JSONB)
    important_covered: Mapped[list | None] = mapped_column(JSONB)
    important_missed: Mapped[list | None] = mapped_column(JSONB)
    optional_missed: Mapped[list | None] = mapped_column(JSONB)
    technical_errors: Mapped[list | None] = mapped_column(JSONB)
    # Legacy columns from the old 0..1 verdict grader; retained for rows graded before
    # the 0..10 rubric so historical data still reads. Not written by the new grader.
    verdict: Mapped[str | None] = mapped_column(String(20))
    covered: Mapped[list | None] = mapped_column(JSONB)
    missing: Mapped[list | None] = mapped_column(JSONB)
    rationale: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()

    __table_args__ = (UniqueConstraint("session_id", "position"),)


class RagQueryLog(Base):
    """Observability for retrieval calls."""
    __tablename__ = "rag_query_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[str | None] = mapped_column(String(128), index=True)
    scope: Mapped[str | None] = mapped_column(String(32))
    query: Mapped[str] = mapped_column(Text)
    retrieved_ids: Mapped[dict | None] = mapped_column(JSONB)
    reranked: Mapped[bool] = mapped_column(default=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    grounded: Mapped[bool | None] = mapped_column()
    created_at: Mapped[datetime] = _now()

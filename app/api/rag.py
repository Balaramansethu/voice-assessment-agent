"""RAG REST surface. These endpoints back the agent tools (Phase 3) and are also
directly callable for inspection/eval. Scope and visibility are enforced here so no
caller can reach internal rubrics or another candidate's documents."""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import RagQueryLog
from app.db.session import get_session
from app.rag import generate, retriever

router = APIRouter(prefix="/rag", tags=["rag"])


class KBSearchRequest(BaseModel):
    query: str
    role: str | None = None
    session_id: str | None = None


class KBAnswerRequest(BaseModel):
    query: str
    role: str | None = None
    session_id: str | None = None


class CandidateContextRequest(BaseModel):
    candidate_id: int
    query: str = "background experience skills"


def _log(session: Session, *, scope, query, chunks, latency_ms, grounded, session_id):
    session.add(RagQueryLog(
        session_id=session_id, scope=scope, query=query,
        retrieved_ids={"ids": [c.chunk_id for c in chunks]},
        latency_ms=latency_ms, grounded=grounded,
    ))


@router.post("/kb/search")
def kb_search(body: KBSearchRequest, session: Session = Depends(get_session)) -> dict:
    """Candidate-visible KB retrieval (inspection/eval). Never returns internal rubrics."""
    t0 = time.time()
    chunks = retriever.search_kb(session, body.query, visibility="candidate", role=body.role)
    latency = int((time.time() - t0) * 1000)
    _log(session, scope="kb", query=body.query, chunks=chunks, latency_ms=latency,
         grounded=None, session_id=body.session_id)
    return {
        "results": [
            {"title": c.title, "heading": c.heading, "similarity": round(c.similarity, 3),
             "score": round(c.score, 4), "content": c.content}
            for c in chunks
        ],
        "latency_ms": latency,
    }


@router.post("/kb/answer")
def kb_answer(body: KBAnswerRequest, session: Session = Depends(get_session)) -> dict:
    """Grounded answer for a candidate question. Escalates when context is weak."""
    t0 = time.time()
    chunks = retriever.search_kb(session, body.query, visibility="candidate", role=body.role)
    best = max((c.similarity for c in chunks), default=0.0)

    if not chunks or best < settings.rag_min_score:
        latency = int((time.time() - t0) * 1000)
        _log(session, scope="kb_answer", query=body.query, chunks=chunks,
             latency_ms=latency, grounded=False, session_id=body.session_id)
        return {
            "answer": None, "grounded": False, "escalate": True,
            "message": "I don't have that information — let me connect you with the "
                       "recruiting team.",
            "citations": [], "latency_ms": latency, "best_similarity": round(best, 3),
        }

    text = generate.answer(body.query, chunks)
    latency = int((time.time() - t0) * 1000)
    _log(session, scope="kb_answer", query=body.query, chunks=chunks,
         latency_ms=latency, grounded=True, session_id=body.session_id)
    return {
        "answer": text, "grounded": True, "escalate": False,
        "citations": [{"title": c.title, "heading": c.heading, "uri": c.uri} for c in chunks],
        "latency_ms": latency, "best_similarity": round(best, 3),
    }


@router.post("/candidate/context")
def candidate_context(body: CandidateContextRequest,
                      session: Session = Depends(get_session)) -> dict:
    """Candidate's OWN documents only (candidate-visible). Row-filtered by id."""
    chunks = retriever.search_candidate(session, body.query, candidate_id=body.candidate_id,
                                        visibility="candidate")
    return {
        "candidate_id": body.candidate_id,
        "documents": [{"doc_type": c.title, "similarity": round(c.similarity, 3),
                       "content": c.content} for c in chunks],
    }

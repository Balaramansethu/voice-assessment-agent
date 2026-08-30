"""Hybrid retrieval: dense (pgvector cosine) + sparse (Postgres full-text), fused
with Reciprocal Rank Fusion. Scope and visibility are enforced in SQL, never
post-hoc — a candidate query can only reach candidate-visible rows.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import CandidateDocument, KBChunk, KBSource
from app.observability.tracing import traceable
from app.rag.embeddings import get_embedder

_RRF_K = 60


@dataclass
class RetrievedChunk:
    chunk_id: int
    content: str
    title: str
    heading: str | None
    uri: str | None
    score: float          # fused RRF score
    similarity: float     # best dense cosine similarity (0..1)


def _rrf(dense: list[int], sparse: list[int]) -> dict[int, float]:
    """Reciprocal Rank Fusion over two ranked id lists."""
    scores: dict[int, float] = {}
    for ranking in (dense, sparse):
        for rank, cid in enumerate(ranking, start=1):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (_RRF_K + rank)
    return scores


@traceable(run_type="retriever", name="search_kb")
def search_kb(
    session: Session,
    query: str,
    *,
    visibility: str = "candidate",
    role: str | None = None,
    top_k: int | None = None,
    final_k: int | None = None,
) -> list[RetrievedChunk]:
    top_k = top_k or settings.rag_top_k
    final_k = final_k or settings.rag_final_k
    qvec = get_embedder().embed_query(query)

    base_filters = [KBSource.visibility == visibility]
    if role:
        base_filters.append((KBSource.role == role) | (KBSource.role.is_(None)))

    # Dense: cosine distance (lower = closer)
    dist = KBChunk.embedding.cosine_distance(qvec)
    dense_rows = session.execute(
        select(KBChunk.id, dist.label("d"))
        .join(KBSource, KBSource.id == KBChunk.source_id)
        .where(*base_filters)
        .order_by(dist)
        .limit(top_k)
    ).all()
    dense_ids = [r.id for r in dense_rows]
    sim_by_id = {r.id: 1.0 - float(r.d) for r in dense_rows}

    # Sparse: full-text rank
    tsv = func.to_tsvector("english", KBChunk.content)
    tsq = func.plainto_tsquery("english", query)
    sparse_rows = session.execute(
        select(KBChunk.id)
        .join(KBSource, KBSource.id == KBChunk.source_id)
        .where(tsv.op("@@")(tsq), *base_filters)
        .order_by(func.ts_rank_cd(tsv, tsq).desc())
        .limit(top_k)
    ).all()
    sparse_ids = [r.id for r in sparse_rows]

    return _assemble(session, _rrf(dense_ids, sparse_ids), sim_by_id, final_k)


@traceable(run_type="retriever", name="search_candidate")
def search_candidate(
    session: Session,
    query: str,
    *,
    candidate_id: int,
    visibility: str = "candidate",
    final_k: int | None = None,
) -> list[RetrievedChunk]:
    """Candidate-scoped retrieval — always filtered by candidate_id."""
    final_k = final_k or settings.rag_final_k
    qvec = get_embedder().embed_query(query)
    dist = CandidateDocument.embedding.cosine_distance(qvec)
    rows = session.execute(
        select(CandidateDocument.id, CandidateDocument.content,
               CandidateDocument.doc_type, dist.label("d"))
        .where(CandidateDocument.candidate_id == candidate_id,
               CandidateDocument.visibility == visibility)
        .order_by(dist)
        .limit(final_k)
    ).all()
    return [
        RetrievedChunk(chunk_id=r.id, content=r.content, title=r.doc_type,
                       heading=None, uri=None, score=1.0 - float(r.d),
                       similarity=1.0 - float(r.d))
        for r in rows
    ]


@traceable(run_type="retriever", name="get_rubric_for_question")
def get_rubric_for_question(session: Session, question: str) -> list[RetrievedChunk]:
    """Internal-only: fetch the rubric chunks for a specific interview question
    (used by the scoring path, never surfaced to the candidate)."""
    src = session.scalar(
        select(KBSource).where(KBSource.type == "rubric", KBSource.visibility == "internal")
        .join(KBChunk, KBChunk.source_id == KBSource.id)
        .where(KBChunk.meta["question"].astext == question)
    )
    if src is None:
        # fall back to semantic match against internal rubrics
        return search_kb(session, question, visibility="internal", final_k=settings.rag_final_k)
    chunks = session.scalars(
        select(KBChunk).where(KBChunk.source_id == src.id).order_by(KBChunk.ordinal)
    ).all()
    return [RetrievedChunk(chunk_id=c.id, content=c.content, title=src.title,
                           heading=c.heading, uri=src.uri, score=1.0, similarity=1.0)
            for c in chunks]


def _assemble(session: Session, fused: dict[int, float], sim_by_id: dict[int, float],
              final_k: int) -> list[RetrievedChunk]:
    if not fused:
        return []
    top_ids = [cid for cid, _ in sorted(fused.items(), key=lambda kv: kv[1], reverse=True)[:final_k]]
    rows = session.execute(
        select(KBChunk.id, KBChunk.content, KBChunk.heading, KBSource.title, KBSource.uri)
        .join(KBSource, KBSource.id == KBChunk.source_id)
        .where(KBChunk.id.in_(top_ids))
    ).all()
    by_id = {r.id: r for r in rows}
    out = []
    for cid in top_ids:
        r = by_id.get(cid)
        if r is None:
            continue
        out.append(RetrievedChunk(
            chunk_id=cid, content=r.content, title=r.title, heading=r.heading,
            uri=r.uri, score=fused[cid], similarity=sim_by_id.get(cid, 0.0),
        ))
    return out

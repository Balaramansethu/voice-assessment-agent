"""RAG retrieval + the security-critical isolation properties.

Runs against the real Postgres/pgvector (inside the api container):
    docker compose exec api pytest tests/rag
"""
import pytest
from sqlalchemy import select

from app.db.models import Candidate, CandidateDocument
from app.db.session import SessionLocal, init_db
from app.rag import retriever
from app.rag.embeddings import get_embedder
from app.rag.ingest import ingest_dir


@pytest.fixture(scope="module")
def session():
    init_db()
    s = SessionLocal()
    ingest_dir(s, "data")          # idempotent; embeddings cached
    s.commit()
    yield s
    s.close()


def test_kb_retrieval_finds_relevant_chunk(session):
    hits = retriever.search_kb(session, "what is the remote work policy?", visibility="candidate")
    assert hits, "expected retrieval results"
    assert any("Remote" in (h.heading or "") or "remote" in h.content.lower() for h in hits)


def test_internal_rubric_never_in_candidate_scope(session):
    # Even explicitly asking for rubric content must not surface internal rubrics.
    hits = retriever.search_kb(session, "rubric score anchors model answer database",
                               visibility="candidate")
    assert all("Rubric" not in h.title for h in hits), "internal rubric leaked to candidate scope"


def test_rubric_retrievable_for_scoring(session):
    chunks = retriever.get_rubric_for_question(session, "How do you approach database schema design?")
    assert chunks and any("Rubric" in c.title for c in chunks)


def test_candidate_document_isolation(session):
    # Create candidate B with a distinctive doc; ensure A cannot retrieve it.
    b = Candidate(name="Isolation Test B", phone="+910000000099")
    session.add(b)
    session.flush()
    vec = get_embedder().embed_documents(["UNIQUE_SECRET_TOKEN_XYZ backend experience"])[0]
    session.add(CandidateDocument(candidate_id=b.id, doc_type="resume",
                                  visibility="candidate", content="UNIQUE_SECRET_TOKEN_XYZ",
                                  embedding=vec))
    session.flush()

    rahul = session.scalar(select(Candidate).where(Candidate.phone == "+919000000001"))
    # Query as Rahul for B's secret — must not return B's doc.
    a_hits = retriever.search_candidate(session, "UNIQUE_SECRET_TOKEN_XYZ",
                                        candidate_id=rahul.id if rahul else 1)
    assert all("UNIQUE_SECRET_TOKEN_XYZ" not in h.content for h in a_hits)

    # B can retrieve their own doc.
    b_hits = retriever.search_candidate(session, "UNIQUE_SECRET_TOKEN_XYZ", candidate_id=b.id)
    assert any("UNIQUE_SECRET_TOKEN_XYZ" in h.content for h in b_hits)

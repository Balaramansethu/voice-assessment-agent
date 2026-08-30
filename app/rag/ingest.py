"""Ingestion: markdown corpora → chunks → embeddings → Postgres (pgvector).

Idempotent: a source is keyed by its file path (uri). If the content checksum is
unchanged the source is skipped; if it changed, old chunks are replaced and the
version is bumped. Candidate documents are bound to a candidate by phone.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db.models import Candidate, CandidateDocument, KBChunk, KBSource
from app.observability.tracing import traceable
from app.rag.embeddings import get_embedder
from app.services.candidate_resolver import normalize_phone


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse a leading `--- key: value ---` block. Returns (meta, body)."""
    meta: dict = {}
    if not text.startswith("---"):
        return meta, text
    end = text.find("\n---", 3)
    if end == -1:
        return meta, text
    block = text[3:end].strip()
    body = text[end + 4:].lstrip("\n")
    for line in block.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip()
    return meta, body


def chunk_markdown(body: str, max_chars: int = 1400) -> list[tuple[str | None, str]]:
    """Heading-aware chunking. Each `##`/`###` section is a chunk; long sections
    are split on paragraph boundaries. Returns (heading, content) pairs."""
    sections: list[tuple[str | None, list[str]]] = []
    current_heading: str | None = None
    current: list[str] = []
    for line in body.splitlines():
        if line.startswith("## ") or line.startswith("### "):
            if current:
                sections.append((current_heading, current))
            current_heading = line.lstrip("# ").strip()
            current = [line]
        else:
            current.append(line)
    if current:
        sections.append((current_heading, current))

    chunks: list[tuple[str | None, str]] = []
    for heading, lines in sections:
        text = "\n".join(lines).strip()
        if not text:
            continue
        if len(text) <= max_chars:
            chunks.append((heading, text))
            continue
        # split oversized section on blank lines
        buf = ""
        for para in text.split("\n\n"):
            if len(buf) + len(para) > max_chars and buf:
                chunks.append((heading, buf.strip()))
                buf = ""
            buf += para + "\n\n"
        if buf.strip():
            chunks.append((heading, buf.strip()))
    return chunks


def _checksum(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _upsert_candidate_by_phone(session: Session, phone: str, name: str) -> Candidate:
    phone = normalize_phone(phone)
    cand = session.scalar(select(Candidate).where(Candidate.phone == phone))
    if cand is None:
        cand = Candidate(name=name, phone=phone)
        session.add(cand)
        session.flush()
    return cand


@traceable(run_type="chain", name="ingest.ingest_file")
def ingest_file(session: Session, path: Path, *, reindex: bool = False) -> dict:
    raw = path.read_text()
    meta, body = parse_frontmatter(raw)
    uri = str(path)
    checksum = _checksum(body)
    embedder = get_embedder()

    if meta.get("type") == "candidate":
        return _ingest_candidate(session, meta, body, embedder)

    # KB source (company/role/faq/rubric)
    existing = session.scalar(select(KBSource).where(KBSource.uri == uri))
    if existing and existing.checksum == checksum and not reindex:
        return {"uri": uri, "status": "unchanged"}

    if existing:
        session.execute(delete(KBChunk).where(KBChunk.source_id == existing.id))
        existing.checksum = checksum
        existing.version += 1
        existing.title = meta.get("title", existing.title)
        existing.visibility = meta.get("visibility", "candidate")
        existing.role = meta.get("role") or None
        source = existing
    else:
        source = KBSource(
            type=meta.get("type", "company"),
            title=meta.get("title", path.stem),
            uri=uri,
            role=meta.get("role") or None,
            visibility=meta.get("visibility", "candidate"),
            checksum=checksum,
        )
        session.add(source)
    session.flush()

    chunks = chunk_markdown(body)
    vectors = embedder.embed_documents([c for _, c in chunks])
    for ordinal, ((heading, content), vec) in enumerate(zip(chunks, vectors)):
        session.add(KBChunk(
            source_id=source.id, ordinal=ordinal, content=content, heading=heading,
            token_count=len(content.split()), embedding=vec,
            meta={"question": meta.get("question")} if meta.get("question") else None,
        ))
    session.flush()
    return {"uri": uri, "status": "ingested", "chunks": len(chunks),
            "visibility": source.visibility}


def _ingest_candidate(session: Session, meta: dict, body: str, embedder) -> dict:
    phone = meta.get("candidate_phone", "")
    # Derive a clean person name from the doc title ("Rahul Sharma — Resume" → "Rahul Sharma").
    clean_name = meta.get("candidate_name") or meta.get("title", "Candidate").split("—")[0].strip()
    cand = _upsert_candidate_by_phone(session, phone, clean_name)
    doc_type = meta.get("doc_type", "resume")
    # replace existing doc of this type for idempotency
    session.execute(delete(CandidateDocument).where(
        CandidateDocument.candidate_id == cand.id,
        CandidateDocument.doc_type == doc_type,
    ))
    vec = embedder.embed_documents([body])[0]
    session.add(CandidateDocument(
        candidate_id=cand.id, doc_type=doc_type,
        visibility=meta.get("visibility", "candidate"),
        content=body, embedding=vec,
        meta={"title": meta.get("title")},
    ))
    session.flush()
    return {"candidate_id": cand.id, "doc_type": doc_type, "status": "ingested"}


def ingest_dir(session: Session, root: str = "data", *, reindex: bool = False) -> list[dict]:
    results = []
    for path in sorted(Path(root).rglob("*.md")):
        results.append(ingest_file(session, path, reindex=reindex))
    return results

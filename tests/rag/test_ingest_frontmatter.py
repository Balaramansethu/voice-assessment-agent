"""Regression tests for frontmatter-only KB ingest changes.

Runs against the real Postgres/pgvector database inside the api container:
    docker compose exec api pytest tests/rag/test_ingest_frontmatter.py -v
"""
from pathlib import Path

import pytest
from sqlalchemy import select

from app.config import settings
from app.db.models import KBChunk, KBSource
from app.db.session import SessionLocal, init_db
from app.rag import ingest as ingest_module
from app.rag import retriever as retriever_module
from app.rag.ingest import ingest_file


BODY = "The body stays byte-for-byte identical across frontmatter edits.\n"


class _TestEmbedder:
    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector() for _ in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector()

    def _vector(self) -> list[float]:
        return [1.0] + [0.0] * (settings.embedding_dim - 1)


@pytest.fixture
def session(monkeypatch):
    init_db()
    embedder = _TestEmbedder()
    monkeypatch.setattr(ingest_module, "get_embedder", lambda: embedder)
    monkeypatch.setattr(retriever_module, "get_embedder", lambda: embedder)
    s = SessionLocal()
    try:
        yield s
    finally:
        s.rollback()
        s.close()


def _write_kb_file(
    path: Path,
    *,
    source_type: str = "company",
    title: str = "Frontmatter Regression",
    visibility: str = "candidate",
    role: str | None = None,
    question: str | None = None,
    body: str = BODY,
) -> None:
    frontmatter = [
        "---",
        f"type: {source_type}",
        f"title: {title}",
        f"visibility: {visibility}",
    ]
    if role is not None:
        frontmatter.append(f"role: {role}")
    if question is not None:
        frontmatter.append(f"question: {question}")
    frontmatter.extend(["---", body])
    path.write_text("\n".join(frontmatter))


def _source_for(session, path: Path) -> KBSource:
    source = session.scalar(select(KBSource).where(KBSource.uri == str(path)))
    assert source is not None
    return source


def _source_snapshot(source: KBSource) -> dict:
    return {
        "type": source.type,
        "title": source.title,
        "role": source.role,
        "visibility": source.visibility,
        "version": source.version,
        "checksum": source.checksum,
    }


def _chunk_ids_for(session, source: KBSource) -> list[int]:
    return list(
        session.scalars(
            select(KBChunk.id)
            .where(KBChunk.source_id == source.id)
            .order_by(KBChunk.ordinal)
        )
    )


def test_first_ingest_stores_candidate_visibility(session, tmp_path):
    path = tmp_path / "frontmatter_first_ingest.md"
    _write_kb_file(path, visibility="candidate")

    result = ingest_file(session, path)

    source = _source_for(session, path)
    assert result["status"] == "ingested"
    assert result["visibility"] == "candidate"
    assert source.visibility == "candidate"


def test_frontmatter_only_visibility_change_updates_existing_source(session, tmp_path):
    path = tmp_path / "frontmatter_visibility.md"
    _write_kb_file(path, visibility="candidate")
    first_result = ingest_file(session, path)
    first_source = _source_for(session, path)
    first_version = first_source.version

    _write_kb_file(path, visibility="internal")
    second_result = ingest_file(session, path)

    source = _source_for(session, path)
    assert first_result["status"] == "ingested"
    assert second_result["status"] != "unchanged"
    assert source.visibility == "internal"
    assert source.version == first_version + 1


@pytest.mark.parametrize(
    ("initial_meta", "updated_meta", "field", "expected"),
    [
        (
            {"role": "Backend Engineer"},
            {"role": "Data Engineer"},
            "role",
            "Data Engineer",
        ),
        (
            {"title": "Original Title"},
            {"title": "Updated Title"},
            "title",
            "Updated Title",
        ),
        (
            {"source_type": "company"},
            {"source_type": "rubric"},
            "type",
            "rubric",
        ),
    ],
)
def test_frontmatter_only_metadata_change_refreshes_stored_field(
    session,
    tmp_path,
    initial_meta,
    updated_meta,
    field,
    expected,
):
    path = tmp_path / f"frontmatter_{field}.md"
    _write_kb_file(path, **initial_meta)
    first_result = ingest_file(session, path)

    _write_kb_file(path, **updated_meta)
    second_result = ingest_file(session, path)

    source = _source_for(session, path)
    assert first_result["status"] == "ingested"
    assert second_result["status"] != "unchanged"
    assert getattr(source, field) == expected


def test_byte_identical_reingest_is_unchanged_and_does_not_modify_source(
    session,
    tmp_path,
):
    path = tmp_path / "frontmatter_unchanged.md"
    _write_kb_file(
        path,
        source_type="rubric",
        title="Stable Rubric",
        visibility="internal",
        role="Backend Engineer",
    )
    first_result = ingest_file(session, path)
    source = _source_for(session, path)
    before = _source_snapshot(source)
    chunk_ids_before = _chunk_ids_for(session, source)

    second_result = ingest_file(session, path)

    source = _source_for(session, path)
    assert first_result["status"] == "ingested"
    assert second_result == {"uri": str(path), "status": "unchanged"}
    assert _source_snapshot(source) == before
    assert _chunk_ids_for(session, source) == chunk_ids_before


def test_internal_lookup_finds_moved_rubric(session, tmp_path):
    path = tmp_path / "frontmatter_moved_rubric.md"
    title = "Moved Internal Rubric"
    body = (
        "Rubric guidance for eigenvector handoff reviews and "
        "deterministic queue drain calibration.\n"
    )
    query = "eigenvector handoff deterministic queue drain calibration"
    question = "How do you keep queue drains deterministic?"
    _write_kb_file(
        path,
        source_type="rubric",
        title=title,
        visibility="candidate",
        question=question,
        body=body,
    )
    first_result = ingest_file(session, path)

    _write_kb_file(
        path,
        source_type="rubric",
        title=title,
        visibility="internal",
        question=question,
        body=body,
    )
    second_result = ingest_file(session, path, reindex=False)

    hits = retriever_module.search_kb(session, query, visibility="internal")

    assert first_result["status"] == "ingested"
    assert second_result["status"] != "unchanged"
    assert any(hit.title == title for hit in hits)

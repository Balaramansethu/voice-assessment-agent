"""Embeddings adapter (provider boundary). Default: fastembed — local ONNX, CPU,
no GPU, zero-paid. Swap the provider without touching retrieval/ingestion code.

fastembed normalizes output vectors, so cosine similarity == dot product, which is
what the pgvector cosine (`vector_cosine_ops`) index expects.
"""
from __future__ import annotations

from functools import lru_cache

from app.config import settings
from app.observability.tracing import traceable


class Embedder:
    """Thin interface so the provider is swappable."""

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError

    def embed_query(self, text: str) -> list[float]:
        raise NotImplementedError

    @property
    def dim(self) -> int:
        return settings.embedding_dim


class FastEmbedEmbedder(Embedder):
    def __init__(self, model: str | None = None):
        from fastembed import TextEmbedding  # deferred: heavy import
        self._model_name = model or settings.embedding_model
        self._model = TextEmbedding(model_name=self._model_name)

    @traceable(run_type="embedding", name="embed_documents")
    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [v.tolist() for v in self._model.embed(texts)]

    @traceable(run_type="embedding", name="embed_query")
    def embed_query(self, text: str) -> list[float]:
        # bge models want a query prefix for retrieval; fastembed's query_embed
        # applies the model-appropriate instruction automatically.
        return next(self._model.query_embed(text)).tolist()


@lru_cache(maxsize=1)
def get_embedder() -> Embedder:
    if settings.embedding_provider == "fastembed":
        return FastEmbedEmbedder()
    raise ValueError(f"unknown embedding_provider: {settings.embedding_provider}")

"""Chroma vector store wrapper for Layer 3 deepening.

Single collection ``layered_awareness_v1`` per profile, persisted at
``<profile_home>/profile_bootstrap/vector/``. Uses Chroma's PersistentClient
in sqlite mode (default) — no external services required.

Wrapper is intentionally narrow: ``upsert`` + ``query``. Distance metric
left as Chroma default (cosine for HNSW). Top-K query returns
:class:`VectorMatch` records.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

_DEFAULT_COLLECTION = "layered_awareness_v1"


class ChromaUnavailableError(RuntimeError):
    """Raised when chromadb isn't installed."""


@dataclass(frozen=True, slots=True)
class VectorMatch:
    """One nearest-neighbour result."""

    id: str
    distance: float
    metadata: dict[str, Any]
    document: str = ""


def _import_chromadb() -> Any:
    """Indirect import so tests can patch easily."""
    import chromadb  # type: ignore[import-not-found]
    return chromadb


def is_chroma_available() -> bool:
    """Cheap probe — only checks that the package is importable."""
    try:
        _import_chromadb()
        return True
    except ImportError:
        return False


class VectorStoreClient:
    """Profile-scoped Chroma client. Creates/opens a single collection."""

    def __init__(
        self,
        *,
        persist_dir: Path,
        collection_name: str = _DEFAULT_COLLECTION,
    ) -> None:
        try:
            chromadb = _import_chromadb()
        except ImportError as exc:
            raise ChromaUnavailableError(
                "chromadb not installed; install via 'pip install opencomputer[deepening]'"
            ) from exc
        persist_dir.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(path=str(persist_dir))
        self._collection = self._client.get_or_create_collection(name=collection_name)

    def upsert(
        self,
        *,
        ids: list[str],
        embeddings: list[list[float]],
        metadatas: list[dict[str, Any]],
        documents: list[str],
    ) -> None:
        """Add or update embeddings. Empty batch is a no-op."""
        if not ids:
            return
        self._collection.upsert(
            ids=ids,
            embeddings=embeddings,
            metadatas=metadatas,
            documents=documents,
        )

    def query(
        self,
        *,
        query_embedding: list[float],
        top_k: int = 5,
    ) -> list[VectorMatch]:
        """Top-K nearest by cosine distance."""
        raw = self._collection.query(
            query_embeddings=[query_embedding],
            n_results=top_k,
        )
        ids = (raw.get("ids") or [[]])[0]
        distances = (raw.get("distances") or [[]])[0]
        metadatas = (raw.get("metadatas") or [[]])[0]
        documents = (raw.get("documents") or [[]])[0]
        out: list[VectorMatch] = []
        for i, doc_id in enumerate(ids):
            out.append(
                VectorMatch(
                    id=str(doc_id),
                    distance=float(distances[i] if i < len(distances) else 0.0),
                    metadata=metadatas[i] if i < len(metadatas) else {},
                    document=str(documents[i] if i < len(documents) else ""),
                )
            )
        return out

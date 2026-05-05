"""ChromaDB-backed vector memory backend (Phase 12d.3 / C.1 MVP, 2026-05-05).

Storage: ``<profile-home>/memory-vector/chroma.db`` (PersistentClient).
Embeddings: ChromaDB's built-in sentence-transformers default
(``all-MiniLM-L6-v2`` lazy-installed via ``chromadb[embeddings]``).

MVP scope: add, search, delete, count, clear. Reindex / eviction /
distributed sharding / cross-profile sharing are explicitly out of
scope (see README).

Pure-logic interface (no I/O at import time) so the host can defer
lazy-loading + tests can mock the chromadb client.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Hit:
    """Single search hit returned by ``search()``."""

    id: str
    text: str
    score: float  # distance — lower is better
    metadata: dict


class ChromaUnavailableError(RuntimeError):
    """ChromaDB isn't installed — install via ``pip install chromadb``."""


class VectorMemoryBackend:
    """Persistent vector memory backed by a single ChromaDB collection.

    One collection per backend instance. Pass ``client_factory`` to inject
    a fake client for tests (signature: ``factory(persist_dir) -> client``).
    """

    DEFAULT_COLLECTION = "opencomputer_vector_memory"

    def __init__(
        self,
        *,
        persist_dir: Path,
        collection_name: str = DEFAULT_COLLECTION,
        client_factory=None,
    ) -> None:
        self.persist_dir = persist_dir
        self.collection_name = collection_name
        self._client = None
        self._collection = None
        self._client_factory = client_factory

    def _ensure_open(self):
        if self._client is not None:
            return
        if self._client_factory is not None:
            self._client = self._client_factory(self.persist_dir)
        else:
            try:
                import chromadb  # type: ignore
            except ImportError as e:
                raise ChromaUnavailableError(
                    "chromadb is not installed. Install with "
                    "'pip install chromadb' to use the memory-vector plugin."
                ) from e
            self.persist_dir.mkdir(parents=True, exist_ok=True)
            self._client = chromadb.PersistentClient(path=str(self.persist_dir))
        self._collection = self._client.get_or_create_collection(
            self.collection_name
        )

    # ─── Public API ─────────────────────────────────────────────────

    def add(self, text: str, metadata: dict | None = None, doc_id: str | None = None) -> str:
        """Store a text chunk + optional metadata. Returns the doc id."""
        self._ensure_open()
        if not text or not text.strip():
            raise ValueError("text must be non-empty")
        the_id = doc_id or uuid.uuid4().hex
        meta = dict(metadata or {})
        meta.setdefault("added_at", int(time.time()))
        self._collection.add(
            ids=[the_id], documents=[text], metadatas=[meta]
        )
        return the_id

    def search(self, query: str, top_k: int = 5) -> list[Hit]:
        """Semantic-search the collection. Returns up to ``top_k`` hits."""
        self._ensure_open()
        if top_k <= 0:
            return []
        results = self._collection.query(
            query_texts=[query], n_results=top_k
        )
        hits: list[Hit] = []
        ids = (results.get("ids") or [[]])[0]
        docs = (results.get("documents") or [[]])[0]
        metas = (results.get("metadatas") or [[]])[0]
        dists = (results.get("distances") or [[]])[0]
        for i in range(len(ids)):
            hits.append(
                Hit(
                    id=ids[i],
                    text=docs[i] if i < len(docs) else "",
                    score=float(dists[i]) if i < len(dists) else 0.0,
                    metadata=dict(metas[i]) if i < len(metas) and metas[i] else {},
                )
            )
        return hits

    def delete(self, doc_id: str) -> bool:
        """Delete a doc by id. Returns True if it existed (best-effort)."""
        self._ensure_open()
        existing = self._collection.get(ids=[doc_id])
        if not (existing.get("ids") or []):
            return False
        self._collection.delete(ids=[doc_id])
        return True

    def count(self) -> int:
        """How many documents currently in the collection."""
        self._ensure_open()
        return int(self._collection.count())

    def clear(self) -> None:
        """Drop everything in the collection (NOT the underlying DB file)."""
        self._ensure_open()
        all_ids = self._collection.get().get("ids") or []
        if all_ids:
            self._collection.delete(ids=all_ids)


__all__ = [
    "ChromaUnavailableError",
    "Hit",
    "VectorMemoryBackend",
]

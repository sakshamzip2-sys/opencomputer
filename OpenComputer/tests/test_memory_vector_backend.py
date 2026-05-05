"""Tests for extensions/memory-vector/backend.py (C.1 MVP).

Uses a fake ChromaDB client so the suite doesn't pull the real
chromadb dependency at test time.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _load_backend():
    import sys

    name = "memory_vector_backend_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name,
        Path(__file__).resolve().parent.parent
        / "extensions"
        / "memory-vector"
        / "backend.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # dataclass needs module in sys.modules
    spec.loader.exec_module(mod)
    return mod


# ─── Fake ChromaDB client ─────────────────────────────────────────────


class _FakeCollection:
    """In-memory dict-backed stand-in for chromadb.Collection."""

    def __init__(self, name: str):
        self.name = name
        self._docs: dict[str, dict] = {}

    def add(self, *, ids, documents, metadatas):
        for i, did in enumerate(ids):
            self._docs[did] = {
                "text": documents[i],
                "metadata": metadatas[i] if metadatas else {},
            }

    def query(self, *, query_texts, n_results):
        # Simplest possible "search": substring match → distance = 1/length-of-overlap.
        q = (query_texts or [""])[0].lower()
        scored: list[tuple[str, float]] = []
        for did, d in self._docs.items():
            text = d["text"].lower()
            if q and q in text:
                scored.append((did, 0.1))  # close-match
            elif text:
                scored.append((did, 1.0))  # weak-match
        scored.sort(key=lambda x: x[1])
        scored = scored[:n_results]
        ids = [s[0] for s in scored]
        return {
            "ids": [ids],
            "documents": [[self._docs[i]["text"] for i in ids]],
            "metadatas": [[self._docs[i]["metadata"] for i in ids]],
            "distances": [[s[1] for s in scored]],
        }

    def get(self, *, ids=None):
        keys = (
            list(self._docs.keys())
            if ids is None
            else [k for k in ids if k in self._docs]
        )
        return {"ids": keys}

    def delete(self, *, ids):
        for did in ids:
            self._docs.pop(did, None)

    def count(self):
        return len(self._docs)


class _FakeClient:
    def __init__(self, persist_dir):
        self.persist_dir = persist_dir
        self._collections: dict[str, _FakeCollection] = {}

    def get_or_create_collection(self, name):
        if name not in self._collections:
            self._collections[name] = _FakeCollection(name)
        return self._collections[name]


def _factory(persist_dir):
    return _FakeClient(persist_dir)


# ─── Tests ────────────────────────────────────────────────────────────


def test_add_then_search_roundtrip(tmp_path: Path):
    mod = _load_backend()
    b = mod.VectorMemoryBackend(persist_dir=tmp_path / "v1", client_factory=_factory)
    doc_id = b.add("the quick brown fox", metadata={"tags": ["animal"]})
    assert isinstance(doc_id, str) and len(doc_id) > 0

    hits = b.search("brown", top_k=5)
    assert any(h.id == doc_id for h in hits)
    found = next(h for h in hits if h.id == doc_id)
    assert "fox" in found.text
    assert found.metadata.get("tags") == ["animal"]


def test_count_reflects_writes(tmp_path: Path):
    mod = _load_backend()
    b = mod.VectorMemoryBackend(persist_dir=tmp_path / "v2", client_factory=_factory)
    assert b.count() == 0
    b.add("first")
    b.add("second")
    assert b.count() == 2


def test_top_k_caps_results(tmp_path: Path):
    mod = _load_backend()
    b = mod.VectorMemoryBackend(persist_dir=tmp_path / "v3", client_factory=_factory)
    for i in range(10):
        b.add(f"doc number {i}")
    hits = b.search("doc", top_k=3)
    assert len(hits) <= 3


def test_delete_returns_true_when_existed(tmp_path: Path):
    mod = _load_backend()
    b = mod.VectorMemoryBackend(persist_dir=tmp_path / "v4", client_factory=_factory)
    doc_id = b.add("ephemeral note")
    assert b.delete(doc_id) is True
    assert b.delete(doc_id) is False  # second delete = idempotent no-op


def test_profile_isolation_two_directories_independent(tmp_path: Path):
    """Two backends with separate persist_dirs share no state."""
    mod = _load_backend()
    a = mod.VectorMemoryBackend(persist_dir=tmp_path / "p1", client_factory=_factory)
    b = mod.VectorMemoryBackend(persist_dir=tmp_path / "p2", client_factory=_factory)
    a.add("only in profile A")
    assert a.count() == 1
    assert b.count() == 0


def test_add_rejects_empty_text(tmp_path: Path):
    mod = _load_backend()
    b = mod.VectorMemoryBackend(persist_dir=tmp_path / "v5", client_factory=_factory)
    with pytest.raises(ValueError):
        b.add("")
    with pytest.raises(ValueError):
        b.add("   ")


def test_clear_removes_everything(tmp_path: Path):
    mod = _load_backend()
    b = mod.VectorMemoryBackend(persist_dir=tmp_path / "v6", client_factory=_factory)
    b.add("a")
    b.add("b")
    assert b.count() == 2
    b.clear()
    assert b.count() == 0


def test_chroma_unavailable_raises_clear_error(tmp_path: Path):
    """When chromadb isn't installed AND no factory is supplied, raise."""
    mod = _load_backend()
    b = mod.VectorMemoryBackend(persist_dir=tmp_path / "v7")  # no factory
    # Force the import inside _ensure_open() to fail by monkeypatching sys.modules
    import sys

    saved = sys.modules.get("chromadb", "__missing__")
    sys.modules["chromadb"] = None  # type: ignore[assignment]
    try:
        with pytest.raises(mod.ChromaUnavailableError):
            b.add("text")
    finally:
        if saved == "__missing__":
            sys.modules.pop("chromadb", None)
        else:
            sys.modules["chromadb"] = saved

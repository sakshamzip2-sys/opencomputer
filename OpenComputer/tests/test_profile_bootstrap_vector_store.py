"""Chroma vector store wrapper tests — mocks chromadb."""
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from opencomputer.profile_bootstrap.vector_store import (
    ChromaUnavailableError,
    VectorStoreClient,
    is_chroma_available,
)


def test_is_chroma_available_returns_false_without_dep():
    with patch(
        "opencomputer.profile_bootstrap.vector_store._import_chromadb",
        side_effect=ImportError(),
    ):
        assert is_chroma_available() is False


def test_client_init_raises_when_unavailable(tmp_path: Path):
    with patch(
        "opencomputer.profile_bootstrap.vector_store._import_chromadb",
        side_effect=ImportError(),
    ):
        with pytest.raises(ChromaUnavailableError):
            VectorStoreClient(persist_dir=tmp_path)


def test_client_upsert_then_query_returns_matches(tmp_path: Path):
    fake_chromadb = MagicMock()
    fake_collection = MagicMock()
    fake_chromadb.PersistentClient.return_value.get_or_create_collection.return_value = (
        fake_collection
    )
    fake_collection.query.return_value = {
        "ids": [["doc1"]],
        "distances": [[0.05]],
        "metadatas": [[{"kind": "file", "source_path": "/a"}]],
        "documents": [["hello world"]],
    }

    with patch(
        "opencomputer.profile_bootstrap.vector_store._import_chromadb",
        return_value=fake_chromadb,
    ):
        client = VectorStoreClient(persist_dir=tmp_path)
        client.upsert(
            ids=["doc1"],
            embeddings=[[0.1] * 384],
            metadatas=[{"kind": "file", "source_path": "/a"}],
            documents=["hello world"],
        )
        results = client.query(query_embedding=[0.1] * 384, top_k=1)

    assert len(results) == 1
    assert results[0].id == "doc1"
    assert results[0].distance == 0.05
    assert results[0].metadata["kind"] == "file"


def test_client_upsert_handles_empty_batch(tmp_path: Path):
    fake_chromadb = MagicMock()
    fake_collection = MagicMock()
    fake_chromadb.PersistentClient.return_value.get_or_create_collection.return_value = (
        fake_collection
    )

    with patch(
        "opencomputer.profile_bootstrap.vector_store._import_chromadb",
        return_value=fake_chromadb,
    ):
        client = VectorStoreClient(persist_dir=tmp_path)
        client.upsert(ids=[], embeddings=[], metadatas=[], documents=[])

    fake_collection.upsert.assert_not_called()

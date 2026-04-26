"""BGE embedder tests — uses sentence-transformers as optional dep.

Tests mock the model so they pass on machines without the heavy weights
downloaded. Real BGE smoke is exercised manually via the doctor check.
"""
from unittest.mock import MagicMock, patch

import pytest

from opencomputer.profile_bootstrap.embedding import (
    EmbeddingUnavailable,
    embed_texts,
    is_embedding_available,
)


def test_is_embedding_available_returns_false_without_dep():
    with patch(
        "opencomputer.profile_bootstrap.embedding._import_sentence_transformers",
        side_effect=ImportError(),
    ):
        assert is_embedding_available() is False


def test_embed_texts_raises_when_unavailable():
    with patch(
        "opencomputer.profile_bootstrap.embedding._import_sentence_transformers",
        side_effect=ImportError(),
    ):
        with pytest.raises(EmbeddingUnavailable):
            embed_texts(["hello"])


def test_embed_texts_returns_vectors_when_available():
    fake_st = MagicMock()
    fake_model = MagicMock()
    fake_model.encode.return_value = [[0.1] * 384, [0.2] * 384]
    fake_st.SentenceTransformer.return_value = fake_model

    with patch(
        "opencomputer.profile_bootstrap.embedding._import_sentence_transformers",
        return_value=fake_st,
    ):
        vecs = embed_texts(["hello", "world"])

    assert len(vecs) == 2
    assert len(vecs[0]) == 384


def test_embed_texts_handles_empty_input():
    fake_st = MagicMock()
    with patch(
        "opencomputer.profile_bootstrap.embedding._import_sentence_transformers",
        return_value=fake_st,
    ):
        vecs = embed_texts([])
    assert vecs == []


def test_embed_texts_caches_model_across_calls():
    """Loading BGE is expensive; the helper should cache the model."""
    fake_st = MagicMock()
    fake_model = MagicMock()
    fake_model.encode.return_value = [[0.0] * 384]
    fake_st.SentenceTransformer.return_value = fake_model

    with patch(
        "opencomputer.profile_bootstrap.embedding._import_sentence_transformers",
        return_value=fake_st,
    ):
        from opencomputer.profile_bootstrap import embedding as emb_mod
        # Reset the module-level cache so we observe the load.
        emb_mod._cached_model = None
        embed_texts(["a"])
        embed_texts(["b"])

    # SentenceTransformer should be constructed only once.
    assert fake_st.SentenceTransformer.call_count == 1

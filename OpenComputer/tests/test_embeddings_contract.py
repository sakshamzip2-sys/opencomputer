"""Embedding-API SDK contract tests (v1.1 plan-3 M6.6)."""

import pytest

from plugin_sdk import (
    MAX_EMBED_BATCH_SIZE,
    BaseProvider,
    EmbeddingBatch,
    EmbeddingsUnsupportedError,
    Message,
    ProviderResponse,
)


def test_embedding_batch_dimensionality_consistency() -> None:
    batch = EmbeddingBatch(
        vectors=[[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]],
        dimensionality=3,
        model_id="test-model",
    )
    assert len(batch.vectors) == 2
    assert batch.dimensionality == 3
    assert batch.model_id == "test-model"
    assert batch.cost_estimate_usd == 0.0
    assert batch.prompt_tokens is None


def test_embedding_batch_rejects_dimensionality_mismatch_first_vector() -> None:
    with pytest.raises(ValueError, match="dimensionality=3.*length 4"):
        EmbeddingBatch(
            vectors=[[0.1, 0.2, 0.3, 0.4]],
            dimensionality=3,
            model_id="test-model",
        )


def test_embedding_batch_rejects_dimensionality_mismatch_other_vector() -> None:
    with pytest.raises(ValueError, match="vector at index 1.*length 2"):
        EmbeddingBatch(
            vectors=[[0.1, 0.2, 0.3], [0.4, 0.5]],
            dimensionality=3,
            model_id="test-model",
        )


def test_embedding_batch_empty_vectors_ok() -> None:
    batch = EmbeddingBatch(
        vectors=[],
        dimensionality=512,
        model_id="test-model",
    )
    assert batch.vectors == []
    assert batch.dimensionality == 512


def test_embedding_batch_rejects_negative_cost() -> None:
    with pytest.raises(ValueError, match="cost_estimate_usd cannot be negative"):
        EmbeddingBatch(
            vectors=[[0.1]],
            dimensionality=1,
            model_id="test-model",
            cost_estimate_usd=-0.001,
        )


def test_embedding_batch_optional_metadata_default_empty_dict() -> None:
    batch = EmbeddingBatch(
        vectors=[[0.1]],
        dimensionality=1,
        model_id="m",
    )
    assert batch.metadata == {}


def test_max_embed_batch_size_constant_is_100() -> None:
    assert MAX_EMBED_BATCH_SIZE == 100


def test_baseprovider_default_embed_raises_not_supported() -> None:
    """A provider that does not override embed() raises EmbeddingsUnsupportedError.
    The vector index relies on this for graceful degradation."""

    class _StubProvider(BaseProvider):
        name = "stub"

        async def complete(self, **kw):  # type: ignore[no-untyped-def]
            return ProviderResponse(message=Message(role="assistant", content=""))

        async def stream_complete(self, **kw):  # type: ignore[no-untyped-def]
            if False:
                yield None  # pragma: no cover

    import asyncio

    p = _StubProvider()
    with pytest.raises(EmbeddingsUnsupportedError, match="stub"):
        asyncio.run(p.embed(["hello"]))


def test_embeddings_not_supported_is_subclass_of_exception() -> None:
    assert issubclass(EmbeddingsUnsupportedError, Exception)


def test_embedding_batch_is_frozen_slotted() -> None:
    batch = EmbeddingBatch(
        vectors=[[0.1]],
        dimensionality=1,
        model_id="m",
    )
    with pytest.raises((AttributeError, Exception)):
        batch.dimensionality = 2  # type: ignore[misc]

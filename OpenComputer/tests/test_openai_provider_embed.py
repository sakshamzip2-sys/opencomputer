"""OpenAI provider embed() tests with a mocked AsyncOpenAI client (v1.1 plan-3 M6.6)."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from plugin_sdk import EmbeddingBatch
from plugin_sdk.embeddings import MAX_BATCH_SIZE


def _load_openai_provider_module() -> Any:
    """Load the OpenAI provider with a unique synthetic module name (matches
    the loader's collision-avoidance pattern) so this test can construct the
    class directly without registering the plugin first."""
    repo_root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "_oai_provider_test_only",
        repo_root / "extensions" / "openai-provider" / "provider.py",
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _set_dummy_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-dummy")


def _make_response(vectors: list[list[float]], prompt_tokens: int = 0) -> Any:
    """Build a fake AsyncOpenAI embeddings response object."""
    response = MagicMock()
    response.data = [MagicMock(embedding=v) for v in vectors]
    response.usage = MagicMock(prompt_tokens=prompt_tokens)
    return response


@pytest.mark.asyncio
async def test_embed_single_text_returns_batch_with_correct_shape() -> None:
    mod = _load_openai_provider_module()
    provider = mod.OpenAIProvider()

    fake_vec = [0.1] * 1536  # text-embedding-3-small default dimensionality
    provider.client = MagicMock()
    provider.client.embeddings = MagicMock()
    provider.client.embeddings.create = AsyncMock(
        return_value=_make_response([fake_vec], prompt_tokens=8)
    )

    batch = await provider.embed(["hello world"])

    assert isinstance(batch, EmbeddingBatch)
    assert len(batch.vectors) == 1
    assert batch.dimensionality == 1536
    assert batch.model_id == "text-embedding-3-small"
    assert batch.prompt_tokens == 8
    assert batch.cost_estimate_usd > 0.0


@pytest.mark.asyncio
async def test_embed_multiple_texts_preserves_order() -> None:
    mod = _load_openai_provider_module()
    provider = mod.OpenAIProvider()

    vecs = [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6], [0.7, 0.8, 0.9]]
    provider.client = MagicMock()
    provider.client.embeddings = MagicMock()
    provider.client.embeddings.create = AsyncMock(
        return_value=_make_response(vecs, prompt_tokens=12)
    )

    batch = await provider.embed(["alpha", "beta", "gamma"])
    assert batch.vectors == vecs
    assert batch.dimensionality == 3


@pytest.mark.asyncio
async def test_embed_chunks_when_over_max_batch_size() -> None:
    mod = _load_openai_provider_module()
    provider = mod.OpenAIProvider()

    # 250 inputs → 3 chunks (100 + 100 + 50)
    n = 250
    inputs = [f"text-{i}" for i in range(n)]

    call_count = {"n": 0}

    async def fake_create(*, model: str, input: list[str]) -> Any:
        call_count["n"] += 1
        return _make_response([[0.1, 0.2, 0.3] for _ in input], prompt_tokens=len(input))

    provider.client = MagicMock()
    provider.client.embeddings = MagicMock()
    provider.client.embeddings.create = fake_create

    batch = await provider.embed(inputs)

    assert call_count["n"] == 3
    assert len(batch.vectors) == n
    assert batch.dimensionality == 3
    assert batch.prompt_tokens == n


@pytest.mark.asyncio
async def test_embed_empty_list_returns_empty_batch_no_api_call() -> None:
    mod = _load_openai_provider_module()
    provider = mod.OpenAIProvider()

    provider.client = MagicMock()
    provider.client.embeddings = MagicMock()
    provider.client.embeddings.create = AsyncMock(side_effect=AssertionError("must not be called"))

    batch = await provider.embed([])
    assert batch.vectors == []
    assert batch.dimensionality == 1536
    assert batch.cost_estimate_usd == 0.0
    assert batch.prompt_tokens == 0


@pytest.mark.asyncio
async def test_embed_caller_specified_model_overrides_default() -> None:
    mod = _load_openai_provider_module()
    provider = mod.OpenAIProvider()

    captured: dict[str, Any] = {}

    async def fake_create(*, model: str, input: list[str]) -> Any:
        captured["model"] = model
        return _make_response([[1.0] * 3072], prompt_tokens=4)

    provider.client = MagicMock()
    provider.client.embeddings = MagicMock()
    provider.client.embeddings.create = fake_create

    batch = await provider.embed(["large doc"], model="text-embedding-3-large")
    assert captured["model"] == "text-embedding-3-large"
    assert batch.model_id == "text-embedding-3-large"
    assert batch.dimensionality == 3072


@pytest.mark.asyncio
async def test_embed_rejects_response_with_wrong_count() -> None:
    mod = _load_openai_provider_module()
    provider = mod.OpenAIProvider()

    # Server returns 2 vectors but we asked for 3 — must raise
    provider.client = MagicMock()
    provider.client.embeddings = MagicMock()
    provider.client.embeddings.create = AsyncMock(
        return_value=_make_response([[0.1, 0.2], [0.3, 0.4]], prompt_tokens=8)
    )

    with pytest.raises(RuntimeError, match="returned 2 vectors for 3 inputs"):
        await provider.embed(["a", "b", "c"])


@pytest.mark.asyncio
async def test_embed_rejects_heterogeneous_dimensionality() -> None:
    mod = _load_openai_provider_module()
    provider = mod.OpenAIProvider()

    provider.client = MagicMock()
    provider.client.embeddings = MagicMock()
    provider.client.embeddings.create = AsyncMock(
        return_value=_make_response([[0.1, 0.2, 0.3], [0.4, 0.5]], prompt_tokens=8)
    )

    with pytest.raises(RuntimeError, match="heterogeneous dimensionality"):
        await provider.embed(["a", "b"])


@pytest.mark.asyncio
async def test_embed_cost_estimate_pricing_table() -> None:
    mod = _load_openai_provider_module()
    provider = mod.OpenAIProvider()

    # 1 million tokens at $0.02 per million
    provider.client = MagicMock()
    provider.client.embeddings = MagicMock()
    provider.client.embeddings.create = AsyncMock(
        return_value=_make_response([[0.1] * 1536], prompt_tokens=1_000_000)
    )

    batch = await provider.embed(["x"])
    assert batch.cost_estimate_usd == pytest.approx(0.02, rel=1e-6)


@pytest.mark.asyncio
async def test_embed_unknown_model_zero_cost() -> None:
    mod = _load_openai_provider_module()
    provider = mod.OpenAIProvider()

    provider.client = MagicMock()
    provider.client.embeddings = MagicMock()
    provider.client.embeddings.create = AsyncMock(
        return_value=_make_response([[0.1] * 64], prompt_tokens=10)
    )

    batch = await provider.embed(["x"], model="local-mystery-model")
    # No price entry → cost estimate is 0
    assert batch.cost_estimate_usd == 0.0
    assert batch.model_id == "local-mystery-model"


def test_max_batch_size_is_what_provider_uses() -> None:
    mod = _load_openai_provider_module()
    # Sanity: the SDK cap is the source of truth referenced by the provider.
    assert MAX_BATCH_SIZE == mod.MAX_EMBED_BATCH_SIZE

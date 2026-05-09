"""Anthropic provider embed() tests with a mocked Voyage HTTP client (v1.1 plan-3 M6.6)."""

from __future__ import annotations

import importlib.util
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from plugin_sdk import EmbeddingBatch, EmbeddingsUnsupportedError


def _load_anthropic_provider_module() -> Any:
    repo_root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "_anthropic_provider_test_only",
        repo_root / "extensions" / "anthropic-provider" / "provider.py",
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _set_dummy_anthropic_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-dummy")


def _make_voyage_response(
    vectors: list[list[float]], total_tokens: int = 0, status: int = 200
) -> Any:
    """Build a fake httpx response object for Voyage embeddings."""
    response = MagicMock()
    response.status_code = status
    response.text = "ok"
    response.json = MagicMock(
        return_value={
            "data": [{"embedding": v, "index": i} for i, v in enumerate(vectors)],
            "usage": {"total_tokens": total_tokens},
            "model": "voyage-3-lite",
        }
    )
    return response


def _patch_voyage_http(provider_module: Any, response_factory) -> None:
    """Patch httpx.AsyncClient inside the provider module to a controllable
    async-context-manager fake.

    ``response_factory`` is called with each post() invocation and returns
    the response object.
    """

    @asynccontextmanager
    async def fake_client(*_args, **_kwargs):
        client = MagicMock()
        client.post = AsyncMock(side_effect=response_factory)
        yield client

    provider_module.httpx = MagicMock()
    provider_module.httpx.AsyncClient = fake_client


@pytest.mark.asyncio
async def test_embed_raises_not_supported_when_voyage_key_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    mod = _load_anthropic_provider_module()
    provider = mod.AnthropicProvider()

    with pytest.raises(EmbeddingsUnsupportedError, match="VOYAGE_API_KEY"):
        await provider.embed(["hello"])


@pytest.mark.asyncio
async def test_embed_voyage_happy_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VOYAGE_API_KEY", "voyage-key-test")
    mod = _load_anthropic_provider_module()
    provider = mod.AnthropicProvider()

    fake_vec = [0.1] * 512  # voyage-3-lite default dim

    async def factory(*args, **kwargs):
        return _make_voyage_response([fake_vec], total_tokens=8)

    _patch_voyage_http(mod, factory)

    batch = await provider.embed(["hello"])
    assert isinstance(batch, EmbeddingBatch)
    assert len(batch.vectors) == 1
    assert batch.dimensionality == 512
    assert batch.model_id == "voyage-3-lite"
    assert batch.prompt_tokens == 8
    assert batch.cost_estimate_usd > 0


@pytest.mark.asyncio
async def test_embed_voyage_empty_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VOYAGE_API_KEY", "voyage-key-test")
    mod = _load_anthropic_provider_module()
    provider = mod.AnthropicProvider()

    async def factory(*args, **kwargs):
        raise AssertionError("must not be called for empty input")

    _patch_voyage_http(mod, factory)

    batch = await provider.embed([])
    assert batch.vectors == []
    assert batch.dimensionality == 512
    assert batch.cost_estimate_usd == 0.0


@pytest.mark.asyncio
async def test_embed_voyage_chunks_over_max_batch_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VOYAGE_API_KEY", "voyage-key-test")
    mod = _load_anthropic_provider_module()
    provider = mod.AnthropicProvider()

    n = 220  # 3 chunks: 100 + 100 + 20
    inputs = [f"text-{i}" for i in range(n)]

    call_count = {"n": 0}

    async def factory(url, *args, **kwargs):
        call_count["n"] += 1
        body = kwargs.get("json") or {}
        chunk_n = len(body.get("input") or [])
        return _make_voyage_response(
            [[0.1] * 64 for _ in range(chunk_n)], total_tokens=chunk_n
        )

    _patch_voyage_http(mod, factory)

    batch = await provider.embed(inputs)
    assert call_count["n"] == 3
    assert len(batch.vectors) == n
    assert batch.dimensionality == 64
    assert batch.prompt_tokens == n


@pytest.mark.asyncio
async def test_embed_voyage_http_error_raises_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VOYAGE_API_KEY", "voyage-key-test")
    mod = _load_anthropic_provider_module()
    provider = mod.AnthropicProvider()

    async def factory(*args, **kwargs):
        return _make_voyage_response([], status=500)

    _patch_voyage_http(mod, factory)

    with pytest.raises(RuntimeError, match="HTTP 500"):
        await provider.embed(["hello"])


@pytest.mark.asyncio
async def test_embed_voyage_returns_count_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VOYAGE_API_KEY", "voyage-key-test")
    mod = _load_anthropic_provider_module()
    provider = mod.AnthropicProvider()

    async def factory(*args, **kwargs):
        return _make_voyage_response([[0.1] * 64], total_tokens=4)

    _patch_voyage_http(mod, factory)

    with pytest.raises(RuntimeError, match="returned 1 vectors for 2 inputs"):
        await provider.embed(["a", "b"])


@pytest.mark.asyncio
async def test_embed_voyage_caller_chooses_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VOYAGE_API_KEY", "voyage-key-test")
    mod = _load_anthropic_provider_module()
    provider = mod.AnthropicProvider()

    captured: dict[str, Any] = {}

    async def factory(url, *args, **kwargs):
        body = kwargs.get("json") or {}
        captured["model"] = body.get("model")
        return _make_voyage_response([[0.5] * 1024], total_tokens=4)

    _patch_voyage_http(mod, factory)

    batch = await provider.embed(["hi"], model="voyage-3-large")
    assert captured["model"] == "voyage-3-large"
    assert batch.model_id == "voyage-3-large"
    assert batch.dimensionality == 1024


@pytest.mark.asyncio
async def test_embed_voyage_pricing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VOYAGE_API_KEY", "voyage-key-test")
    mod = _load_anthropic_provider_module()
    provider = mod.AnthropicProvider()

    async def factory(*args, **kwargs):
        return _make_voyage_response([[0.1] * 512], total_tokens=1_000_000)

    _patch_voyage_http(mod, factory)

    batch = await provider.embed(["x"])
    # voyage-3-lite is $0.02/M tokens
    assert batch.cost_estimate_usd == pytest.approx(0.02, rel=1e-6)


@pytest.mark.asyncio
async def test_embed_voyage_unknown_model_zero_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VOYAGE_API_KEY", "voyage-key-test")
    mod = _load_anthropic_provider_module()
    provider = mod.AnthropicProvider()

    async def factory(*args, **kwargs):
        return _make_voyage_response([[0.1] * 32], total_tokens=10)

    _patch_voyage_http(mod, factory)

    batch = await provider.embed(["x"], model="some-future-model")
    assert batch.cost_estimate_usd == 0.0
    assert batch.model_id == "some-future-model"


@pytest.mark.asyncio
async def test_embed_voyage_rejects_heterogeneous_dimensionality(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VOYAGE_API_KEY", "voyage-key-test")
    mod = _load_anthropic_provider_module()
    provider = mod.AnthropicProvider()

    async def factory(*args, **kwargs):
        return _make_voyage_response([[0.1, 0.2, 0.3], [0.4, 0.5]], total_tokens=4)

    _patch_voyage_http(mod, factory)

    with pytest.raises(RuntimeError, match="heterogeneous dimensionality"):
        await provider.embed(["a", "b"])

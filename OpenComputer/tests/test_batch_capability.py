"""Tests for the generic batch-processing capability (Subsystem E)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from plugin_sdk.core import Message
from plugin_sdk.provider_contract import (
    BaseProvider,
    BatchRequest,
    BatchResult,
    BatchUnsupportedError,
    ProviderResponse,
    StreamEvent,
    Usage,
)


def _load_anthropic_provider():
    repo_root = Path(__file__).resolve().parent.parent
    provider_path = repo_root / "extensions" / "anthropic-provider" / "provider.py"
    module_name = f"_anthropic_batch_test_{id(provider_path)}"
    spec = importlib.util.spec_from_file_location(module_name, provider_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


# ─── BaseProvider default raises ──────────────────────────────────


class _StubProvider(BaseProvider):
    """No batch override — should raise BatchUnsupportedError on default."""

    name = "stub"
    default_model = "stub-1"

    async def complete(self, **kwargs: Any) -> ProviderResponse:
        raise NotImplementedError

    async def stream_complete(self, **kwargs: Any):
        if False:
            yield


@pytest.mark.asyncio
async def test_base_provider_submit_batch_raises_unsupported() -> None:
    provider = _StubProvider()
    with pytest.raises(BatchUnsupportedError, match="stub"):
        await provider.submit_batch([])


@pytest.mark.asyncio
async def test_base_provider_get_batch_results_raises_unsupported() -> None:
    provider = _StubProvider()
    with pytest.raises(BatchUnsupportedError, match="stub"):
        await provider.get_batch_results("batch_123")


# ─── BatchRequest dataclass ───────────────────────────────────────


def test_batch_request_defaults() -> None:
    req = BatchRequest(
        custom_id="r1",
        messages=[Message(role="user", content="hi")],
        model="claude-opus-4-7",
    )
    assert req.system == ""
    assert req.max_tokens == 1024
    assert req.runtime_extras is None
    assert req.response_schema is None


def test_batch_request_carries_runtime_and_schema() -> None:
    req = BatchRequest(
        custom_id="r2",
        messages=[Message(role="user", content="hi")],
        model="claude-opus-4-7",
        runtime_extras={"reasoning_effort": "high"},
        response_schema={"name": "x", "schema": {"type": "object"}},
    )
    assert req.runtime_extras["reasoning_effort"] == "high"
    assert req.response_schema["name"] == "x"


# ─── Anthropic submit_batch translation ───────────────────────────


@pytest.mark.asyncio
async def test_anthropic_submit_batch_calls_native_endpoint(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("ANTHROPIC_AUTH_MODE", raising=False)
    mod = _load_anthropic_provider()
    provider = mod.AnthropicProvider()

    fake_batch = MagicMock()
    fake_batch.id = "msgbatch_test123"

    captured: dict = {}

    async def _fake_create(**kwargs):
        captured.update(kwargs)
        return fake_batch

    with patch.object(
        provider.client.messages.batches, "create", side_effect=_fake_create
    ):
        batch_id = await provider.submit_batch(
            [
                BatchRequest(
                    custom_id="r1",
                    messages=[Message(role="user", content="hello")],
                    model="claude-opus-4-7",
                    max_tokens=512,
                ),
            ]
        )

    assert batch_id == "msgbatch_test123"
    assert "requests" in captured
    entries = captured["requests"]
    assert len(entries) == 1
    assert entries[0]["custom_id"] == "r1"
    assert entries[0]["params"]["model"] == "claude-opus-4-7"
    assert entries[0]["params"]["max_tokens"] == 512
    # Opus 4.7 → no temperature kwarg per Subsystem A capability table
    assert "temperature" not in entries[0]["params"]


@pytest.mark.asyncio
async def test_anthropic_submit_batch_carries_effort_and_schema(monkeypatch) -> None:
    """Batch entries should carry per-request effort + schema overrides."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("ANTHROPIC_AUTH_MODE", raising=False)
    mod = _load_anthropic_provider()
    provider = mod.AnthropicProvider()

    fake_batch = MagicMock()
    fake_batch.id = "msgbatch_x"
    captured: dict = {}

    async def _fake_create(**kwargs):
        captured.update(kwargs)
        return fake_batch

    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    with patch.object(
        provider.client.messages.batches, "create", side_effect=_fake_create
    ):
        await provider.submit_batch(
            [
                BatchRequest(
                    custom_id="r2",
                    messages=[Message(role="user", content="x")],
                    model="claude-opus-4-7",
                    runtime_extras={"reasoning_effort": "low"},
                    response_schema={"name": "test", "schema": schema},
                ),
            ]
        )

    params = captured["requests"][0]["params"]
    # Effort lands in output_config.effort (Subsystem B)
    assert params["output_config"]["effort"] == "low"
    # Schema lands in output_config.format (Subsystem C)
    assert params["output_config"]["format"]["schema"] == schema


# ─── Anthropic get_batch_results ──────────────────────────────────


@pytest.mark.asyncio
async def test_anthropic_get_batch_results_processing_returns_pending(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("ANTHROPIC_AUTH_MODE", raising=False)
    mod = _load_anthropic_provider()
    provider = mod.AnthropicProvider()

    fake_batch = MagicMock()
    fake_batch.processing_status = "in_progress"

    async def _fake_retrieve(batch_id):
        return fake_batch

    with patch.object(
        provider.client.messages.batches, "retrieve", side_effect=_fake_retrieve
    ):
        results = await provider.get_batch_results("msgbatch_x")

    assert len(results) == 1
    assert results[0].status == "processing"


@pytest.mark.asyncio
async def test_anthropic_get_batch_results_succeeded(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("ANTHROPIC_AUTH_MODE", raising=False)
    mod = _load_anthropic_provider()
    provider = mod.AnthropicProvider()

    fake_batch = MagicMock()
    fake_batch.processing_status = "ended"

    # Build a successful entry
    fake_text_block = MagicMock()
    fake_text_block.type = "text"
    fake_text_block.text = "Done."
    fake_msg = MagicMock()
    fake_msg.content = [fake_text_block]
    fake_msg.stop_reason = "end_turn"
    fake_msg.usage.input_tokens = 5
    fake_msg.usage.output_tokens = 2
    fake_msg.usage.cache_read_input_tokens = 0
    fake_msg.usage.cache_creation_input_tokens = 0

    fake_result = MagicMock()
    fake_result.type = "succeeded"
    fake_result.message = fake_msg

    fake_entry = MagicMock()
    fake_entry.custom_id = "r1"
    fake_entry.result = fake_result

    async def _fake_retrieve(batch_id):
        return fake_batch

    async def _async_iter():
        yield fake_entry

    async def _fake_results(batch_id):
        return _async_iter()

    with patch.object(
        provider.client.messages.batches, "retrieve", side_effect=_fake_retrieve
    ), patch.object(
        provider.client.messages.batches, "results", side_effect=_fake_results
    ):
        results = await provider.get_batch_results("msgbatch_x")

    assert len(results) == 1
    assert results[0].custom_id == "r1"
    assert results[0].status == "succeeded"
    assert results[0].response is not None
    assert "Done" in results[0].response.message.content

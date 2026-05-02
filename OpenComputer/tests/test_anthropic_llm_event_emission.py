"""Anthropic provider emits LLMCallEvent on every successful completion.

Phase 4 Task 4.3 — completes the provider-side observability story
started by Task 4.4 (OpenAI). Verifies the wiring records events
to the central JSONL sink.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


def _load_anthropic_provider():
    """Load the bundled anthropic-provider module under a unique name.

    Registers in sys.modules BEFORE exec_module so pydantic can resolve
    forward references to ``Literal`` (mirrors test_anthropic_provider_pool.py).
    """
    repo = Path(__file__).resolve().parent.parent
    provider_path = repo / "extensions" / "anthropic-provider" / "provider.py"
    module_name = f"_anthropic_provider_event_test_{id(provider_path)}"
    spec = importlib.util.spec_from_file_location(module_name, str(provider_path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _fake_anthropic_response():
    """Build an anthropic.types.Message-shaped mock."""
    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = "hello"

    resp = MagicMock()
    resp.content = [text_block]
    resp.stop_reason = "end_turn"
    resp.usage = MagicMock()
    resp.usage.input_tokens = 42
    resp.usage.output_tokens = 7
    resp.usage.cache_read_input_tokens = 100
    resp.usage.cache_creation_input_tokens = 25
    return resp


@pytest.mark.asyncio
async def test_complete_emits_llm_call_event(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-irrelevant")
    monkeypatch.setenv("OPENCOMPUTER_PROFILE_HOME", str(tmp_path))

    mod = _load_anthropic_provider()
    provider = mod.AnthropicProvider()

    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=_fake_anthropic_response())
    monkeypatch.setattr(provider, "client", mock_client)
    monkeypatch.setattr(provider, "_credential_pool", None)

    from plugin_sdk.core import Message

    await provider.complete(
        model="claude-sonnet-4-6",
        messages=[Message(role="user", content="hi")],
    )

    log = tmp_path / "llm_events.jsonl"
    assert log.exists(), "LLMCallEvent should have been recorded"
    lines = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    assert len(lines) == 1
    event = lines[0]
    assert event["provider"] == "anthropic"
    assert event["model"] == "claude-sonnet-4-6"
    assert event["input_tokens"] == 42
    assert event["output_tokens"] == 7
    assert event["cache_creation_tokens"] == 25
    assert event["cache_read_tokens"] == 100
    assert event["site"] == "agent_loop"
    assert event["latency_ms"] >= 0
    # Anthropic Sonnet 4-6 is in the pricing table; cost should be present.
    assert event["cost_usd"] is not None
    assert event["cost_usd"] > 0


@pytest.mark.asyncio
async def test_complete_threads_site_kwarg(tmp_path, monkeypatch):
    """Caller-supplied site= must land in the recorded event."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-irrelevant")
    monkeypatch.setenv("OPENCOMPUTER_PROFILE_HOME", str(tmp_path))

    mod = _load_anthropic_provider()
    provider = mod.AnthropicProvider()

    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=_fake_anthropic_response())
    monkeypatch.setattr(provider, "client", mock_client)
    monkeypatch.setattr(provider, "_credential_pool", None)

    from plugin_sdk.core import Message

    await provider.complete(
        model="claude-sonnet-4-6",
        messages=[Message(role="user", content="hi")],
        site="eval_grader",
    )

    log = tmp_path / "llm_events.jsonl"
    lines = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    assert len(lines) == 1
    assert lines[0]["site"] == "eval_grader"


@pytest.mark.asyncio
async def test_emit_swallows_sink_failures(tmp_path, monkeypatch):
    """If record_llm_call raises, the provider must not crash."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-irrelevant")

    mod = _load_anthropic_provider()

    def _exploding(event):
        raise RuntimeError("simulated sink failure")

    monkeypatch.setattr(mod, "record_llm_call", _exploding)

    provider = mod.AnthropicProvider()
    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=_fake_anthropic_response())
    monkeypatch.setattr(provider, "client", mock_client)
    monkeypatch.setattr(provider, "_credential_pool", None)

    from plugin_sdk.core import Message

    response = await provider.complete(
        model="claude-sonnet-4-6",
        messages=[Message(role="user", content="hi")],
    )
    assert response.message.content == "hello"

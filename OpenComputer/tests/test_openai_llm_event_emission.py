"""OpenAI provider emits LLMCallEvent on every successful completion.

Phase 4 Task 4.4 — single source of truth for LLM-call observability is
the provider, not the agent loop. Verifies the wiring records events
to the central JSONL sink.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


def _load_openai_provider():
    """Load the bundled openai-provider module under a unique name."""
    repo = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "_openai_provider_under_test",
        str(repo / "extensions" / "openai-provider" / "provider.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.asyncio
async def test_complete_emits_llm_call_event(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENCOMPUTER_PROFILE_HOME", str(tmp_path))

    mod = _load_openai_provider()
    provider = mod.OpenAIProvider()

    # Mock the SDK call so we don't hit OpenAI.
    fake_choice = MagicMock()
    fake_choice.message = MagicMock(content="hello", tool_calls=None)
    fake_choice.finish_reason = "stop"
    fake_resp = MagicMock()
    fake_resp.choices = [fake_choice]
    fake_resp.usage = MagicMock(prompt_tokens=42, completion_tokens=7)
    fake_resp.usage.prompt_tokens_details = None

    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(return_value=fake_resp)
    monkeypatch.setattr(provider, "client", mock_client)
    monkeypatch.setattr(provider, "_credential_pool", None)

    from plugin_sdk.core import Message

    await provider.complete(
        model="gpt-4o-mini",
        messages=[Message(role="user", content="hi")],
    )

    log = tmp_path / "llm_events.jsonl"
    assert log.exists(), "LLMCallEvent should have been recorded"
    lines = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    assert len(lines) == 1
    event = lines[0]
    assert event["provider"] == "openai"
    assert event["model"] == "gpt-4o-mini"
    assert event["input_tokens"] == 42
    assert event["output_tokens"] == 7
    assert event["site"] == "agent_loop"
    assert event["latency_ms"] >= 0


@pytest.mark.asyncio
async def test_emit_swallows_sink_failures(tmp_path, monkeypatch):
    """If record_llm_call raises, the provider must not crash."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    mod = _load_openai_provider()

    # Patch record_llm_call to raise — simulating disk-full / permission-denied.
    def _exploding_recorder(event):
        raise RuntimeError("simulated sink failure")

    monkeypatch.setattr(mod, "record_llm_call", _exploding_recorder)

    provider = mod.OpenAIProvider()
    fake_choice = MagicMock()
    fake_choice.message = MagicMock(content="ok", tool_calls=None)
    fake_choice.finish_reason = "stop"
    fake_resp = MagicMock()
    fake_resp.choices = [fake_choice]
    fake_resp.usage = MagicMock(prompt_tokens=1, completion_tokens=1)
    fake_resp.usage.prompt_tokens_details = None

    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(return_value=fake_resp)
    monkeypatch.setattr(provider, "client", mock_client)
    monkeypatch.setattr(provider, "_credential_pool", None)

    from plugin_sdk.core import Message

    # Must not raise.
    response = await provider.complete(
        model="gpt-4o-mini",
        messages=[Message(role="user", content="hi")],
    )
    assert response.message.content == "ok"

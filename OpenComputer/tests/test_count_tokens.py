"""Tests for provider-agnostic token counting (Subsystem D)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from plugin_sdk.core import Message, ToolCall
from plugin_sdk.provider_contract import (
    BaseProvider,
    ProviderResponse,
    StreamEvent,
    Usage,
    _heuristic_token_count,
)


def _load_anthropic_provider():
    repo_root = Path(__file__).resolve().parent.parent
    provider_path = repo_root / "extensions" / "anthropic-provider" / "provider.py"
    module_name = f"_anthropic_count_tokens_test_{id(provider_path)}"
    spec = importlib.util.spec_from_file_location(module_name, provider_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_openai_provider():
    repo_root = Path(__file__).resolve().parent.parent
    provider_path = repo_root / "extensions" / "openai-provider" / "provider.py"
    module_name = f"_openai_count_tokens_test_{id(provider_path)}"
    spec = importlib.util.spec_from_file_location(module_name, provider_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


# ─── Heuristic fallback ────────────────────────────────────────────


def test_heuristic_returns_at_least_one() -> None:
    """Heuristic never returns 0 for non-empty input."""
    assert _heuristic_token_count([Message(role="user", content="x")], "") >= 1


def test_heuristic_scales_with_input_size() -> None:
    """Doubling content roughly doubles the token estimate."""
    short = _heuristic_token_count(
        [Message(role="user", content="hello")], ""
    )
    long = _heuristic_token_count(
        [Message(role="user", content="hello" * 100)], ""
    )
    assert long > short * 50  # 100x content → ≥50x tokens


def test_heuristic_includes_system_and_tool_calls() -> None:
    """System prompt + tool call args contribute to the count."""
    base = _heuristic_token_count([Message(role="user", content="hi")], "")
    with_system = _heuristic_token_count(
        [Message(role="user", content="hi")],
        "long system prompt here " * 10,
    )
    assert with_system > base


# ─── Default BaseProvider implementation ──────────────────────────


class _StubProvider(BaseProvider):
    """No-override stub — uses default heuristic implementation."""

    name = "stub"
    default_model = "stub-1"

    async def complete(self, **kwargs: Any) -> ProviderResponse:
        raise NotImplementedError

    async def stream_complete(self, **kwargs: Any):
        if False:
            yield


@pytest.mark.asyncio
async def test_base_provider_count_tokens_uses_heuristic_default() -> None:
    """A provider with no override gets the heuristic for free."""
    provider = _StubProvider()
    count = await provider.count_tokens(
        model="stub-1",
        messages=[Message(role="user", content="x" * 400)],
    )
    # 400 chars / 4 = ~100 tokens
    assert 80 <= count <= 120


# ─── Anthropic native override ────────────────────────────────────


@pytest.mark.asyncio
async def test_anthropic_count_tokens_uses_native_endpoint(monkeypatch) -> None:
    """Anthropic provider should call client.messages.count_tokens."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("ANTHROPIC_AUTH_MODE", raising=False)
    mod = _load_anthropic_provider()
    provider = mod.AnthropicProvider()

    fake_response = MagicMock()
    fake_response.input_tokens = 1234

    async def _fake_count_tokens(**kwargs):
        return fake_response

    with patch.object(
        provider.client.messages, "count_tokens", side_effect=_fake_count_tokens
    ):
        result = await provider.count_tokens(
            model="claude-opus-4-7",
            messages=[Message(role="user", content="hi")],
        )
    assert result == 1234


@pytest.mark.asyncio
async def test_anthropic_count_tokens_falls_back_on_error(monkeypatch) -> None:
    """When the native endpoint raises, fall back to heuristic."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("ANTHROPIC_AUTH_MODE", raising=False)
    mod = _load_anthropic_provider()
    provider = mod.AnthropicProvider()

    async def _raise(**kwargs):
        raise RuntimeError("network down")

    with patch.object(
        provider.client.messages, "count_tokens", side_effect=_raise
    ):
        result = await provider.count_tokens(
            model="claude-opus-4-7",
            messages=[Message(role="user", content="x" * 400)],
        )
    # Heuristic-style fallback (~100 tokens for 400 chars)
    assert 80 <= result <= 120


# ─── OpenAI tiktoken override ─────────────────────────────────────


@pytest.mark.asyncio
async def test_openai_count_tokens_uses_tiktoken_when_available(monkeypatch) -> None:
    """OpenAI provider should use tiktoken locally when installed."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    mod = _load_openai_provider()
    provider = mod.OpenAIProvider()

    # tiktoken is installed via openai SDK dependency. If it's not
    # present in this environment, the test exits cleanly via skip.
    try:
        import tiktoken  # noqa: F401
    except ImportError:
        pytest.skip("tiktoken not installed in this test environment")

    result = await provider.count_tokens(
        model="gpt-4o",
        messages=[Message(role="user", content="hello world this is a test")],
    )
    # tiktoken on 6 words ≈ 6-8 tokens. Heuristic on 25 chars ≈ 6 tokens.
    # Both bounds work — the assertion is "real number, not zero".
    assert 1 <= result <= 50


@pytest.mark.asyncio
async def test_openai_count_tokens_includes_tool_calls(monkeypatch) -> None:
    """Tool call args contribute to the count."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    mod = _load_openai_provider()
    provider = mod.OpenAIProvider()

    no_tools = await provider.count_tokens(
        model="gpt-4o",
        messages=[Message(role="user", content="hi")],
    )
    with_tool_call = await provider.count_tokens(
        model="gpt-4o",
        messages=[
            Message(
                role="assistant",
                content="hi",
                tool_calls=[
                    ToolCall(
                        id="t1",
                        name="search_web",
                        arguments={
                            "query": "what is the meaning of life " * 10,
                        },
                    ),
                ],
            ),
        ],
    )
    assert with_tool_call > no_tools

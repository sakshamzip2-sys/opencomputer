"""Verify provider-side kwargs construction respects model capabilities.

The directory ``extensions/anthropic-provider/`` has a hyphen
(invalid Python module name), so we load the provider via importlib
the same way ``tests/test_anthropic_provider_pool.py`` does.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _load_anthropic_provider():
    """Load AnthropicProvider fresh from disk, bypassing module cache."""
    repo_root = Path(__file__).resolve().parent.parent
    provider_path = repo_root / "extensions" / "anthropic-provider" / "provider.py"
    module_name = f"_anthropic_provider_kwargs_test_{id(provider_path)}"
    spec = importlib.util.spec_from_file_location(module_name, provider_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _stub_response():
    """Build a minimal valid Anthropic response stub."""
    resp = MagicMock()
    resp.content = []
    resp.stop_reason = "end_turn"
    resp.usage.input_tokens = 1
    resp.usage.output_tokens = 1
    resp.usage.cache_read_input_tokens = 0
    resp.usage.cache_creation_input_tokens = 0
    return resp


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    monkeypatch.delenv("ANTHROPIC_AUTH_MODE", raising=False)
    mod = _load_anthropic_provider()
    return mod.AnthropicProvider()


@pytest.mark.asyncio
async def test_opus_4_7_call_omits_temperature(provider) -> None:
    """On Opus 4.7, the kwargs sent to messages.create must NOT contain temperature."""
    captured: dict = {}

    async def _capture(**kw):
        captured.update(kw)
        return _stub_response()

    with patch.object(provider.client.messages, "create", side_effect=_capture):
        await provider.complete(
            model="claude-opus-4-7",
            messages=[],
            max_tokens=100,
            temperature=0.7,
        )
    assert "temperature" not in captured
    assert "top_p" not in captured
    assert "top_k" not in captured


@pytest.mark.asyncio
async def test_opus_4_5_call_includes_temperature(provider) -> None:
    """On Opus 4.5 (legacy), temperature is preserved."""
    captured: dict = {}

    async def _capture(**kw):
        captured.update(kw)
        return _stub_response()

    with patch.object(provider.client.messages, "create", side_effect=_capture):
        await provider.complete(
            model="claude-opus-4-5",
            messages=[],
            max_tokens=100,
            temperature=0.7,
        )
    assert captured.get("temperature") == 0.7


@pytest.mark.asyncio
async def test_high_effort_lifts_max_tokens_floor_on_adaptive(provider) -> None:
    """xhigh effort on Opus 4.7 lifts max_tokens floor to 64000."""
    captured: dict = {}

    async def _capture(**kw):
        captured.update(kw)
        return _stub_response()

    with patch.object(provider.client.messages, "create", side_effect=_capture):
        await provider.complete(
            model="claude-opus-4-7",
            messages=[],
            max_tokens=4096,
            runtime_extras={"reasoning_effort": "xhigh"},
        )
    assert captured["max_tokens"] >= 64000


@pytest.mark.asyncio
async def test_low_effort_does_not_lift_max_tokens(provider) -> None:
    """Low effort doesn't trigger the floor lift."""
    captured: dict = {}

    async def _capture(**kw):
        captured.update(kw)
        return _stub_response()

    with patch.object(provider.client.messages, "create", side_effect=_capture):
        await provider.complete(
            model="claude-opus-4-7",
            messages=[],
            max_tokens=4096,
            runtime_extras={"reasoning_effort": "low"},
        )
    assert captured["max_tokens"] == 4096

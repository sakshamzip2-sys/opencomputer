"""Tests for cross-provider cache_tokens + reasoning surfacing.

Covers Sub-projects A + B of the model-agnosticism plan: every supported
provider populates Usage.cache_read_tokens / cache_write_tokens AND
ProviderResponse.reasoning when the underlying API surfaces them.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).parent.parent
_OPENAI_PROVIDER_PY = _REPO / "extensions" / "openai-provider" / "provider.py"


def _load_openai_module():
    """Load the OpenAI provider module under a unique name to avoid
    sibling-module shadow with the OpenRouter provider (CLAUDE.md §7.1)."""
    sys.modules.pop("provider_agnostic_test", None)
    spec = importlib.util.spec_from_file_location(
        "provider_agnostic_test", _OPENAI_PROVIDER_PY
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["provider_agnostic_test"] = mod
    spec.loader.exec_module(mod)
    return mod


# ── OpenAI: _extract_cached_tokens ───────────────────────────────────


def test_openai_cached_tokens_present():
    mod = _load_openai_module()

    class _Details:
        cached_tokens = 1234

    class _Usage:
        prompt_tokens = 2000
        completion_tokens = 500
        prompt_tokens_details = _Details()

    assert mod._extract_cached_tokens(_Usage()) == 1234


def test_openai_cached_tokens_missing_details():
    mod = _load_openai_module()

    class _Usage:
        prompt_tokens = 100
        completion_tokens = 50

    assert mod._extract_cached_tokens(_Usage()) == 0


def test_openai_cached_tokens_none_details():
    mod = _load_openai_module()

    class _Usage:
        prompt_tokens = 100
        completion_tokens = 50
        prompt_tokens_details = None

    assert mod._extract_cached_tokens(_Usage()) == 0


def test_openai_cached_tokens_none_usage():
    mod = _load_openai_module()
    assert mod._extract_cached_tokens(None) == 0


def test_openai_cached_tokens_zero_normalises():
    """Defensive: cached_tokens=None coerces to 0 (some SDKs return None)."""
    mod = _load_openai_module()

    class _Details:
        cached_tokens = None

    class _Usage:
        prompt_tokens_details = _Details()

    assert mod._extract_cached_tokens(_Usage()) == 0


# ── OpenAI: _extract_reasoning_content ───────────────────────────────


def test_openai_reasoning_present():
    mod = _load_openai_module()

    class _Msg:
        content = "the answer is 42"
        reasoning_content = "Step 1: ...\nStep 2: ..."
        tool_calls = None

    assert mod._extract_reasoning_content(_Msg()) == "Step 1: ...\nStep 2: ..."


def test_openai_reasoning_absent_returns_none():
    mod = _load_openai_module()

    class _Msg:
        content = "answer"
        tool_calls = None

    assert mod._extract_reasoning_content(_Msg()) is None


def test_openai_reasoning_empty_string_returns_none():
    """Empty string normalised to None for consistency with Anthropic."""
    mod = _load_openai_module()

    class _Msg:
        content = "answer"
        reasoning_content = ""

    assert mod._extract_reasoning_content(_Msg()) is None


def test_openai_reasoning_none_msg_returns_none():
    mod = _load_openai_module()
    assert mod._extract_reasoning_content(None) is None


# ── Anthropic: cache_tokens + reasoning extraction ───────────────────


def test_anthropic_parse_response_populates_cache_and_reasoning():
    """The Anthropic provider's _parse_response now surfaces both
    cache_read_input_tokens / cache_creation_input_tokens AND extended-thinking
    blocks. All three were previously dropped on the floor."""
    spec = importlib.util.spec_from_file_location(
        "anthropic_provider_module_test",
        _REPO / "extensions" / "anthropic-provider" / "provider.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["anthropic_provider_module_test"] = mod
    spec.loader.exec_module(mod)

    # Build a fake AnthropicMessage with both content blocks + cache usage.
    class _TextBlock:
        type = "text"
        text = "hello world"

    class _ThinkingBlock:
        type = "thinking"
        thinking = "Let me reason about this step by step..."

    class _ToolBlock:
        type = "tool_use"
        id = "tu_1"
        name = "Read"
        input = {"path": "/tmp/x"}

    class _Usage:
        input_tokens = 100
        output_tokens = 200
        cache_read_input_tokens = 80
        cache_creation_input_tokens = 50

    class _FakeAnthropicMessage:
        content = [_TextBlock(), _ThinkingBlock(), _ToolBlock()]
        stop_reason = "end_turn"
        usage = _Usage()

    # Need a class instance to call _parse_response; bypass __init__ via __new__.
    inst = mod.AnthropicProvider.__new__(mod.AnthropicProvider)
    response = inst._parse_response(_FakeAnthropicMessage())

    # Cache token surfacing
    assert response.usage.cache_read_tokens == 80
    assert response.usage.cache_write_tokens == 50

    # Reasoning surfacing from thinking blocks
    assert response.reasoning is not None
    assert "step by step" in response.reasoning

    # Tool call still parsed correctly
    assert response.message.tool_calls is not None
    assert response.message.tool_calls[0].name == "Read"


def test_anthropic_no_thinking_blocks_reasoning_is_none():
    spec = importlib.util.spec_from_file_location(
        "anthropic_provider_module_test_2",
        _REPO / "extensions" / "anthropic-provider" / "provider.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["anthropic_provider_module_test_2"] = mod
    spec.loader.exec_module(mod)

    class _TextBlock:
        type = "text"
        text = "answer only"

    class _Usage:
        input_tokens = 10
        output_tokens = 20
        # no cache_*_input_tokens fields → getattr returns 0

    class _FakeMsg:
        content = [_TextBlock()]
        stop_reason = "end_turn"
        usage = _Usage()

    inst = mod.AnthropicProvider.__new__(mod.AnthropicProvider)
    response = inst._parse_response(_FakeMsg())
    assert response.reasoning is None
    assert response.usage.cache_read_tokens == 0
    assert response.usage.cache_write_tokens == 0

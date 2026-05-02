"""Anthropic provider declares its capabilities + extracts thinking signatures."""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


def _import_provider():
    """Import the anthropic-provider plugin module despite hyphenated path.

    Cache in ``sys.modules`` BEFORE ``exec_module`` so pydantic can
    resolve forward-referenced types (Literal) when constructing the
    ``AnthropicProviderConfig`` model.
    """
    mod_name = "_anth_provider_caps"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    repo = Path(__file__).resolve().parent.parent
    plugin_path = repo / "extensions" / "anthropic-provider" / "provider.py"
    spec = importlib.util.spec_from_file_location(mod_name, plugin_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_MODE", raising=False)
    mod = _import_provider()
    return mod.AnthropicProvider()


def test_anthropic_capabilities(provider):
    caps = provider.capabilities
    assert caps.requires_reasoning_resend_in_tool_cycle is True
    assert caps.reasoning_block_kind == "anthropic_thinking"
    assert caps.supports_long_ttl is True


def test_anthropic_min_cache_tokens_per_model(provider):
    caps = provider.capabilities
    assert caps.min_cache_tokens("claude-opus-4-7") == 4096
    assert caps.min_cache_tokens("claude-mythos-preview") == 4096
    assert caps.min_cache_tokens("claude-haiku-4-5") == 4096
    assert caps.min_cache_tokens("claude-sonnet-4-6") == 2048
    assert caps.min_cache_tokens("claude-sonnet-4-5") == 1024
    assert caps.min_cache_tokens("claude-3-5-sonnet-latest") == 1024


def test_anthropic_extract_cache_tokens(provider):
    usage = SimpleNamespace(
        input_tokens=10,
        output_tokens=5,
        cache_creation_input_tokens=200,
        cache_read_input_tokens=1234,
    )
    ct = provider.capabilities.extracts_cache_tokens(usage)
    assert ct.read == 1234
    assert ct.write == 200


def test_anthropic_extract_cache_tokens_missing_fields(provider):
    usage = SimpleNamespace(input_tokens=10, output_tokens=5)
    ct = provider.capabilities.extracts_cache_tokens(usage)
    assert ct.read == 0
    assert ct.write == 0


def test_anthropic_parse_response_captures_thinking_signature(provider):
    thinking_block = SimpleNamespace(
        type="thinking",
        thinking="step-by-step reasoning",
        signature="sig-abc-123",
    )
    tool_use = SimpleNamespace(
        type="tool_use",
        id="toolu_01",
        name="Read",
        input={"path": "/etc/hosts"},
    )
    fake_resp = SimpleNamespace(
        content=[thinking_block, tool_use],
        stop_reason="tool_use",
        usage=SimpleNamespace(
            input_tokens=10,
            output_tokens=5,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
    )

    parsed = provider._parse_response(fake_resp)
    assert parsed.reasoning == "step-by-step reasoning"
    assert parsed.reasoning_replay_blocks == [
        {"type": "thinking", "thinking": "step-by-step reasoning", "signature": "sig-abc-123"}
    ]
    # Signature must propagate to canonical Message so SessionDB persists it.
    assert parsed.message.reasoning_replay_blocks == parsed.reasoning_replay_blocks


def test_anthropic_parse_response_thinking_without_signature_skipped(provider):
    """A thinking block with no signature can't be replayed safely; skip."""
    thinking_block = SimpleNamespace(
        type="thinking",
        thinking="some text",
        signature=None,
    )
    fake_resp = SimpleNamespace(
        content=[thinking_block],
        stop_reason="end_turn",
        usage=SimpleNamespace(
            input_tokens=10,
            output_tokens=5,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
    )
    parsed = provider._parse_response(fake_resp)
    assert parsed.reasoning == "some text"  # text still surfaces
    assert parsed.reasoning_replay_blocks is None  # but no replay blocks

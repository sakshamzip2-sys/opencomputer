"""Anthropic provider's _to_anthropic_messages reconstructs thinking blocks
during tool-use cycles per the API's signature contract."""

import importlib.util
import sys
from pathlib import Path

import pytest

from plugin_sdk import Message, ToolCall


def _import_provider():
    mod_name = "_anth_provider_resend"
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
    return _import_provider().AnthropicProvider()


def test_thinking_block_emitted_before_tool_use(provider):
    """Assistant message with tool_calls + reasoning_replay_blocks must
    emit the thinking block on the wire before the tool_use block."""
    msg = Message(
        role="assistant",
        content="reading file now",
        tool_calls=[ToolCall(id="tu_1", name="Read", arguments={"path": "/x"})],
        reasoning_replay_blocks=[
            {"type": "thinking", "thinking": "I should read this file", "signature": "sig-xyz"}
        ],
    )
    wire = provider._to_anthropic_messages([msg])
    assert len(wire) == 1
    blocks = wire[0]["content"]
    # Thinking block must come first — the API checks ordering.
    assert blocks[0]["type"] == "thinking"
    assert blocks[0]["thinking"] == "I should read this file"
    assert blocks[0]["signature"] == "sig-xyz"
    types_after = [b["type"] for b in blocks[1:]]
    assert "tool_use" in types_after


def test_no_thinking_block_when_no_tool_use(provider):
    """Plain assistant text with reasoning_replay_blocks but no tool_calls
    must NOT emit a thinking block (server auto-handles non-cycle turns)."""
    msg = Message(
        role="assistant",
        content="hi",
        tool_calls=None,
        reasoning_replay_blocks=[
            {"type": "thinking", "thinking": "...", "signature": "sig"}
        ],
    )
    wire = provider._to_anthropic_messages([msg])
    # The fall-through branch produces a plain text content (no list of blocks).
    assert wire[0]["content"] == "hi"


def test_no_thinking_block_when_replay_blocks_absent(provider):
    """Tool-use message without reasoning_replay_blocks emits today's shape:
    optional text + tool_use, no thinking block."""
    msg = Message(
        role="assistant",
        content="",
        tool_calls=[ToolCall(id="tu_1", name="Read", arguments={"path": "/x"})],
        reasoning_replay_blocks=None,
    )
    wire = provider._to_anthropic_messages([msg])
    blocks = wire[0]["content"]
    types = [b["type"] for b in blocks]
    assert "thinking" not in types
    assert "tool_use" in types


def test_multiple_thinking_blocks_preserved_in_order(provider):
    """Rare but valid: a response with multiple thinking blocks must emit
    all of them in their original order, before tool_use."""
    msg = Message(
        role="assistant",
        content="",
        tool_calls=[ToolCall(id="tu_1", name="Read", arguments={"path": "/x"})],
        reasoning_replay_blocks=[
            {"type": "thinking", "thinking": "first", "signature": "s1"},
            {"type": "thinking", "thinking": "second", "signature": "s2"},
        ],
    )
    wire = provider._to_anthropic_messages([msg])
    blocks = wire[0]["content"]
    assert blocks[0]["thinking"] == "first"
    assert blocks[1]["thinking"] == "second"
    assert blocks[2]["type"] == "tool_use"


def test_unknown_replay_block_kind_skipped(provider):
    """Defensive: a non-thinking block in replay_blocks must be skipped,
    not propagated to the wire (forward-compat for future extensions)."""
    msg = Message(
        role="assistant",
        content="",
        tool_calls=[ToolCall(id="tu_1", name="Read", arguments={"path": "/x"})],
        reasoning_replay_blocks=[
            {"type": "future_kind", "data": "..."},
            {"type": "thinking", "thinking": "real", "signature": "s"},
        ],
    )
    wire = provider._to_anthropic_messages([msg])
    blocks = wire[0]["content"]
    types = [b["type"] for b in blocks]
    assert "future_kind" not in types
    assert types[0] == "thinking"

"""Context pruning modes — sliding window + cache-TTL strategies.

Pins the production behaviour: tool_use/tool_result pair preservation,
"None mode is a no-op", and the defensive "any crash returns
original messages" contract.
"""
from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any

from opencomputer.agent.context_pruning import (
    ContextPruningConfig,
    prune_messages,
)
from plugin_sdk.core import Message


def _msg(role: str, content: str = "x") -> Message:
    return Message(role=role, content=content)  # type: ignore[arg-type]


def _tool_use(tool_id: str = "t1") -> Message:
    return Message(
        role="assistant",
        content=[
            {"type": "text", "text": "calling tool"},
            {"type": "tool_use", "id": tool_id, "name": "Bash",
             "input": {"command": "ls"}},
        ],  # type: ignore[arg-type]
    )


def _tool_result(tool_id: str = "t1") -> Message:
    return Message(
        role="user",
        content=[
            {"type": "tool_result", "tool_use_id": tool_id, "content": "ok"},
        ],  # type: ignore[arg-type]
    )


# ─── mode=none ────────────────────────────────────────────────────────


def test_none_mode_is_noop():
    msgs = [_msg("user", "hello"), _msg("assistant", "hi")]
    out = prune_messages(msgs, ContextPruningConfig(mode="none"))
    assert out == msgs


def test_empty_input_returns_empty():
    out = prune_messages([], ContextPruningConfig(mode="sliding"))
    assert out == []


# ─── sliding window ───────────────────────────────────────────────────


def test_sliding_keeps_last_n_user_turns():
    msgs = [
        _msg("system", "sys"),
        _msg("user", "u1"), _msg("assistant", "a1"),
        _msg("user", "u2"), _msg("assistant", "a2"),
        _msg("user", "u3"), _msg("assistant", "a3"),
        _msg("user", "u4"), _msg("assistant", "a4"),
    ]
    cfg = ContextPruningConfig(mode="sliding", window_turns=2)
    out = prune_messages(msgs, cfg)
    # System + last 2 user turns + their assistant replies = 5 msgs.
    contents = [m.content for m in out]
    assert "sys" in contents
    assert "u3" in contents and "u4" in contents
    assert "u1" not in contents and "u2" not in contents


def test_sliding_preserves_system_message():
    msgs = [
        _msg("system", "system-prompt"),
        *[_msg("user", f"u{i}") for i in range(20)],
    ]
    cfg = ContextPruningConfig(mode="sliding", window_turns=3)
    out = prune_messages(msgs, cfg)
    assert out[0].content == "system-prompt"


def test_sliding_drops_system_when_disabled():
    msgs = [
        _msg("system", "system-prompt"),
        *[_msg("user", f"u{i}") for i in range(20)],
    ]
    cfg = ContextPruningConfig(
        mode="sliding", window_turns=3, always_keep_system=False,
    )
    out = prune_messages(msgs, cfg)
    # System should NOT be there as a separate head when always_keep_system=False.
    assert all(m.content != "system-prompt" for m in out)


def test_sliding_no_op_when_window_exceeds_turns():
    """Don't prune below floor — return original if window > available turns."""
    msgs = [_msg("user", "u1"), _msg("assistant", "a1"), _msg("user", "u2")]
    cfg = ContextPruningConfig(mode="sliding", window_turns=10)
    out = prune_messages(msgs, cfg)
    assert out == msgs


def test_sliding_preserves_tool_pair_at_boundary():
    """A tool_use just before the cutoff with its tool_result inside
    the kept window must NOT be orphaned."""
    msgs = [
        _msg("user", "u1"),       # 0
        _tool_use("t1"),          # 1: tool_use OUTSIDE window
        _tool_result("t1"),       # 2: tool_result INSIDE window
        _msg("assistant", "a1"),  # 3
        _msg("user", "u2"),       # 4 ← cutoff
        _msg("assistant", "a2"),  # 5
    ]
    cfg = ContextPruningConfig(
        mode="sliding", window_turns=1, always_keep_system=False,
    )
    out = prune_messages(msgs, cfg)
    # The tool_use+tool_result pair must be kept together.
    has_tool_use = any(
        isinstance(m.content, list)
        and any(isinstance(it, dict) and it.get("type") == "tool_use" for it in m.content)
        for m in out
    )
    has_tool_result = any(
        isinstance(m.content, list)
        and any(isinstance(it, dict) and it.get("type") == "tool_result" for it in m.content)
        for m in out
    )
    assert has_tool_use == has_tool_result, (
        f"orphaned tool pair in pruned output: "
        f"has_tool_use={has_tool_use}, has_tool_result={has_tool_result}"
    )


def test_sliding_window_zero_returns_original():
    msgs = [_msg("user", f"u{i}") for i in range(5)]
    cfg = ContextPruningConfig(mode="sliding", window_turns=0)
    out = prune_messages(msgs, cfg)
    assert out == msgs


# ─── cache-ttl ────────────────────────────────────────────────────────


def _stamped(role: str, content: str, ts: float) -> Message:
    """Build a Message-like object that carries an extra ``timestamp``
    attribute. We use a tiny shim subclass since ``Message`` is frozen
    + slotted — direct attribute assignment fails."""

    class _Stamped:
        def __init__(self, role: str, content: Any, ts: float):
            self.role = role
            self.content = content
            self.tool_call_id = None
            self.tool_calls = None
            self.name = None
            self.reasoning = None
            self.reasoning_details = None
            self.codex_reasoning_items = None
            self.reasoning_replay_blocks = None
            self.attachments: list[str] = []
            self.timestamp = ts

    return _Stamped(role, content, ts)  # type: ignore[return-value]


def test_cache_ttl_drops_old_messages():
    now = 1000.0
    msgs = [
        _stamped("user", "old-1", ts=900),     # 100s ago
        _stamped("assistant", "old-1-r", ts=900),
        _stamped("user", "fresh", ts=995),     # 5s ago
        _stamped("assistant", "fresh-r", ts=995),
    ]
    cfg = ContextPruningConfig(mode="cache-ttl", ttl_seconds=60)
    out = prune_messages(msgs, cfg, now=now)
    assert len(out) == 2
    contents = [m.content for m in out]
    assert "fresh" in contents
    assert "old-1" not in contents


def test_cache_ttl_zero_seconds_is_noop():
    msgs = [_stamped("user", "x", ts=0)]
    cfg = ContextPruningConfig(mode="cache-ttl", ttl_seconds=0)
    assert prune_messages(msgs, cfg, now=1000) == msgs


def test_cache_ttl_keeps_messages_without_timestamps():
    """Untimed messages must survive — pruning blind would corrupt history."""
    now = 1000.0
    untimed = _msg("user", "unstamped")
    msgs = [
        _stamped("user", "old", ts=10),
        untimed,
        _stamped("user", "fresh", ts=999),
    ]
    cfg = ContextPruningConfig(mode="cache-ttl", ttl_seconds=60)
    out = prune_messages(msgs, cfg, now=now)
    contents = [getattr(m, "content", None) for m in out]
    assert "unstamped" in contents
    assert "fresh" in contents
    assert "old" not in contents


def test_cache_ttl_keeps_system_message():
    now = 1000.0
    msgs = [
        _stamped("system", "important-system", ts=0),  # very old
        _stamped("user", "fresh", ts=999),
    ]
    cfg = ContextPruningConfig(
        mode="cache-ttl", ttl_seconds=60, always_keep_system=True,
    )
    out = prune_messages(msgs, cfg, now=now)
    contents = [getattr(m, "content", None) for m in out]
    assert "important-system" in contents
    assert "fresh" in contents


def test_cache_ttl_handles_datetime_timestamps():
    """``timestamp`` may be a ``datetime`` object — our extractor copes."""
    import datetime

    fresh = _stamped("user", "x", ts=0)
    fresh.timestamp = datetime.datetime.fromtimestamp(995)  # type: ignore[attr-defined]
    msgs = [fresh, _stamped("user", "old", ts=10)]
    cfg = ContextPruningConfig(mode="cache-ttl", ttl_seconds=60)
    out = prune_messages(msgs, cfg, now=1000)
    contents = [getattr(m, "content", None) for m in out]
    assert "x" in contents
    assert "old" not in contents


def test_cache_ttl_drops_orphan_tool_pair_atomically():
    """If a tool_use-tool_result pair straddles the TTL boundary, drop
    BOTH so the wire stays balanced."""

    class _StampedToolUse:
        def __init__(self, ts: float):
            self.role = "assistant"
            self.content = [
                {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}
            ]
            self.tool_call_id = None
            self.tool_calls = None
            self.name = None
            self.reasoning = None
            self.reasoning_details = None
            self.codex_reasoning_items = None
            self.reasoning_replay_blocks = None
            self.attachments: list[str] = []
            self.timestamp = ts

    class _StampedToolResult:
        def __init__(self, ts: float):
            self.role = "user"
            self.content = [
                {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}
            ]
            self.tool_call_id = None
            self.tool_calls = None
            self.name = None
            self.reasoning = None
            self.reasoning_details = None
            self.codex_reasoning_items = None
            self.reasoning_replay_blocks = None
            self.attachments: list[str] = []
            self.timestamp = ts

    now = 1000.0
    msgs = [
        _StampedToolUse(ts=10),   # OLD — would be dropped
        _StampedToolResult(ts=999),  # FRESH — would be kept
    ]
    cfg = ContextPruningConfig(mode="cache-ttl", ttl_seconds=60)
    out = prune_messages(msgs, cfg, now=now)
    # Both must drop together rather than orphan tool_result.
    assert out == []


# ─── defensive ────────────────────────────────────────────────────────


def test_crashing_strategy_returns_original(monkeypatch, caplog):
    msgs = [_msg("user", "x")]
    cfg = ContextPruningConfig(mode="sliding", window_turns=1)

    def boom(*a, **kw):
        raise RuntimeError("simulated")

    import opencomputer.agent.context_pruning as cp
    monkeypatch.setattr(cp, "_prune_sliding", boom)
    with caplog.at_level(logging.WARNING, logger="opencomputer.agent.context_pruning"):
        out = prune_messages(msgs, cfg)
    assert out == msgs
    assert any("crashed" in r.message for r in caplog.records)


def test_unknown_mode_returns_original():
    msgs = [_msg("user", "x")]
    # Bypass the Literal type by constructing via dataclasses.replace.
    cfg = replace(ContextPruningConfig(), mode="bogus")  # type: ignore[arg-type]
    assert prune_messages(msgs, cfg) == msgs

"""IV.3 — merge_adjacent_user_messages helper.

Kimi CLI merges adjacent user messages before sending to the API, so that
multiple DynamicInjectionProviders firing in one turn produce a single
clean user message on the wire (saves tokens + improves prompt-cache
hit rate).

OpenComputer doesn't currently stack user messages from injections (the
composed injection string is folded into the ``system`` prompt, not the
message list), but the helper is a safety net for any code path that
DOES append a standalone user message — and a pure function is trivially
testable in isolation.

See ``sources/kimi-cli/src/kimi_cli/soul/dynamic_injection.py:40-66``.
"""

from __future__ import annotations

from opencomputer.agent.loop import merge_adjacent_user_messages
from plugin_sdk.core import Message, ToolCall


def test_merge_two_adjacent_text_user_messages() -> None:
    """Two consecutive text-only user messages → merged into one."""
    msgs = [
        Message(role="user", content="first"),
        Message(role="user", content="second"),
    ]
    out = merge_adjacent_user_messages(msgs)
    assert len(out) == 1
    assert out[0].role == "user"
    assert out[0].content == "first\n\nsecond"


def test_merge_three_adjacent_text_user_messages() -> None:
    """Three consecutive text-only user messages → merged into one."""
    msgs = [
        Message(role="user", content="a"),
        Message(role="user", content="b"),
        Message(role="user", content="c"),
    ]
    out = merge_adjacent_user_messages(msgs)
    assert len(out) == 1
    assert out[0].role == "user"
    assert out[0].content == "a\n\nb\n\nc"


def test_tool_result_user_not_merged_with_text_user() -> None:
    """A user-role message carrying a tool_call_id (tool_result) is
    NEVER merged with a plain text user message. (OpenComputer uses
    role='tool' for tool results; but defensive-check the tool_call_id
    field too in case any provider adapter maps differently.)"""
    tool_result_like = Message(
        role="user",
        content="result text",
        tool_call_id="toolu_123",
    )
    text = Message(role="user", content="follow-up question")
    msgs = [tool_result_like, text]
    out = merge_adjacent_user_messages(msgs)
    assert len(out) == 2
    assert out[0] is tool_result_like
    assert out[1] is text


def test_tool_role_message_not_merged() -> None:
    """Tool-role messages (OpenComputer's tool_result carrier) are never
    merged — their role isn't 'user' so they can't stack under the
    merge rule at all. Sanity check that adjacency across tool messages
    doesn't merge surrounding users."""
    msgs = [
        Message(role="user", content="hi"),
        Message(
            role="tool",
            content="tool output",
            tool_call_id="toolu_abc",
            name="Read",
        ),
        Message(role="user", content="ok"),
    ]
    out = merge_adjacent_user_messages(msgs)
    # user → tool → user : three separate messages, no merge.
    assert len(out) == 3
    assert out[0].content == "hi"
    assert out[1].role == "tool"
    assert out[2].content == "ok"


def test_user_assistant_user_not_merged() -> None:
    """User → assistant → user (single): no merge because they're not
    adjacent."""
    msgs = [
        Message(role="user", content="hello"),
        Message(role="assistant", content="hi there"),
        Message(role="user", content="follow up"),
    ]
    out = merge_adjacent_user_messages(msgs)
    assert len(out) == 3
    assert [m.role for m in out] == ["user", "assistant", "user"]


def test_empty_list() -> None:
    """Empty messages list → empty (no crash, no extra allocation surprises)."""
    assert merge_adjacent_user_messages([]) == []


def test_idempotent() -> None:
    """Running the merger twice produces the same output as running once."""
    msgs = [
        Message(role="user", content="a"),
        Message(role="user", content="b"),
        Message(role="assistant", content="ack"),
        Message(role="user", content="c"),
        Message(role="user", content="d"),
    ]
    once = merge_adjacent_user_messages(msgs)
    twice = merge_adjacent_user_messages(once)
    assert once == twice
    # Sanity: the first pass actually did merge something.
    assert len(once) < len(msgs)
    assert len(once) == 3  # ab, assistant, cd


def test_preserves_order_and_non_user_messages() -> None:
    """Merging doesn't shuffle messages or drop non-user content."""
    msgs = [
        Message(role="user", content="u1"),
        Message(role="user", content="u2"),
        Message(role="assistant", content="a1"),
        Message(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id="tc1", name="Read", arguments={"path": "x"})],
        ),
        Message(role="tool", content="file contents", tool_call_id="tc1", name="Read"),
        Message(role="user", content="u3"),
    ]
    out = merge_adjacent_user_messages(msgs)
    assert [m.role for m in out] == ["user", "assistant", "assistant", "tool", "user"]
    assert out[0].content == "u1\n\nu2"
    assert out[1].content == "a1"
    # Assistant-with-tool_calls preserved intact:
    assert out[2].tool_calls is not None
    assert out[2].tool_calls[0].id == "tc1"
    assert out[3].role == "tool"
    assert out[4].content == "u3"


def test_user_with_tool_calls_not_merged() -> None:
    """Defensive: if a user message somehow carries tool_calls (not
    expected in OpenComputer's schema but possible in mixed provider
    adapters), don't merge it — merging would drop the tool_calls
    linkage."""
    weird_user = Message(
        role="user",
        content="text",
        tool_calls=[ToolCall(id="tc1", name="X", arguments={})],
    )
    plain = Message(role="user", content="next")
    out = merge_adjacent_user_messages([weird_user, plain])
    assert len(out) == 2
    assert out[0] is weird_user
    assert out[1] is plain


def test_single_user_message_unchanged() -> None:
    """One user message in, one user message out — identity-preserving
    on the no-op case."""
    msg = Message(role="user", content="only one")
    out = merge_adjacent_user_messages([msg])
    assert len(out) == 1
    assert out[0] is msg  # same object, no copy


def test_no_user_messages() -> None:
    """Only assistant/tool messages → untouched."""
    msgs = [
        Message(role="assistant", content="hi"),
        Message(role="tool", content="x", tool_call_id="tc1", name="R"),
    ]
    out = merge_adjacent_user_messages(msgs)
    assert len(out) == 2
    assert out[0] is msgs[0]
    assert out[1] is msgs[1]

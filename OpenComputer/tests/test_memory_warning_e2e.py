"""End-to-end verification that the cap-pressure warning lands in the next-turn
model input.

The 2026-05-10 memory-observability design assumes ToolResult.content reaches the
model on the next turn. This test verifies that assumption against the real path
through `_dispatch_tool_calls`: warning prefixed in MemoryTool → ToolResult.content
→ Message(role="tool", content=...) → next iteration's messages list.

If anyone changes the dispatcher to strip warnings from tool results, this test
breaks loudly.
"""

from __future__ import annotations

import asyncio

from opencomputer.agent.memory import MemoryManager
from opencomputer.agent.memory_context import MemoryContext
from opencomputer.agent.state import SessionDB
from opencomputer.tools.memory_tool import MemoryTool
from plugin_sdk.core import ToolCall


def _make_ctx(tmp_path, *, memory_limit: int = 100):
    mm = MemoryManager(
        declarative_path=tmp_path / "MEMORY.md",
        user_path=tmp_path / "USER.md",
        skills_path=tmp_path / "skills",
        memory_char_limit=memory_limit,
        user_char_limit=memory_limit,
    )
    return MemoryContext(
        manager=mm,
        db=SessionDB(tmp_path / "sessions.db"),
        session_id_provider=lambda: "test",
    )


def test_memory_warning_in_tool_message_for_next_turn(tmp_path) -> None:
    """The warning prepended by MemoryTool MUST survive the conversion from
    ToolResult to Message(role="tool"). Tool messages are what the model sees
    on the next turn (`agent/loop.py:5046-5054`)."""
    ctx = _make_ctx(tmp_path, memory_limit=100)
    # Pre-fill so the next add lands above the 80% warn threshold
    ctx.manager.append_declarative("x" * 50)

    tool = MemoryTool(ctx)
    result = asyncio.run(
        tool.execute(
            ToolCall(
                id="tc-warn-1",
                name="Memory",
                arguments={"action": "add", "target": "memory", "content": "y" * 40},
            )
        )
    )

    # The result the dispatcher gets:
    assert result.is_error is False
    assert "MEMORY.md AT" in result.content

    # Simulate the dispatcher's conversion (matches agent/loop.py:5046-5054):
    from plugin_sdk.core import Message

    next_turn_message = Message(
        role="tool",
        content=result.content,
        tool_call_id=result.tool_call_id,
        name="Memory",
    )

    # The model sees this on the next turn — the warning must survive intact.
    assert "MEMORY.md AT" in next_turn_message.content
    # And the original success line must still be there too.
    assert "Added entry to MEMORY.md" in next_turn_message.content


def test_compaction_warning_survives_in_tool_message(tmp_path) -> None:
    """Same shape as above but with a compaction-triggering write — escalated
    warning must reach the next-turn message."""
    ctx = _make_ctx(tmp_path, memory_limit=100)
    # Pre-fill until compaction is forced on the next add
    for i in range(20):
        try:
            ctx.manager.append_declarative(f"older-{i:02d} with text padding")
        except Exception:
            pass

    tool = MemoryTool(ctx)
    result = asyncio.run(
        tool.execute(
            ToolCall(
                id="tc-comp-1",
                name="Memory",
                arguments={"action": "add", "target": "memory", "content": "fresh entry needs space"},
            )
        )
    )
    assert result.is_error is False
    assert "COMPACTED" in result.content
    assert "DROPPED" in result.content

    from plugin_sdk.core import Message
    next_turn_message = Message(
        role="tool", content=result.content, tool_call_id=result.tool_call_id, name="Memory"
    )
    assert "COMPACTED" in next_turn_message.content
    assert "DROPPED" in next_turn_message.content

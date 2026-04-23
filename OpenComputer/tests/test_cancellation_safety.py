"""Phase gap-closure §3.1: context mutation is safe under asyncio cancellation.

The agent loop used to persist the assistant message BEFORE dispatching tools,
then persist tool_result messages one at a time AFTER dispatch. If the task was
cancelled mid-dispatch, SQLite held an assistant row with `tool_calls` but no
matching tool_result rows — on resume Anthropic returns 400.

These tests assert the new atomic-batch path:
- Successful turn persists assistant + tool_results together.
- Cancellation mid-dispatch persists neither.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from opencomputer.agent.compaction import CompactionConfig
from opencomputer.agent.config import Config, LoopConfig, MemoryConfig, ModelConfig, SessionConfig
from opencomputer.agent.loop import AgentLoop
from opencomputer.agent.state import SessionDB
from plugin_sdk.core import Message, ToolCall, ToolResult
from plugin_sdk.provider_contract import ProviderResponse, Usage


def _config(tmp: Path) -> Config:
    return Config(
        model=ModelConfig(provider="mock", model="mock-model", max_tokens=1024, temperature=0.0),
        loop=LoopConfig(max_iterations=3, parallel_tools=False),
        session=SessionConfig(db_path=tmp / "sessions.db"),
        memory=MemoryConfig(
            declarative_path=tmp / "MEMORY.md",
            skills_path=tmp / "skills",
        ),
    )


def _assistant_with_tool_call(tool_name: str = "slow_tool") -> Message:
    return Message(
        role="assistant",
        content="calling a tool",
        tool_calls=[ToolCall(id="tc-1", name=tool_name, arguments={"x": 1})],
    )


def _plain_assistant() -> Message:
    return Message(role="assistant", content="done")


def _mock_provider(resp_message: Message) -> MagicMock:
    p = MagicMock()
    p.complete = AsyncMock(
        return_value=ProviderResponse(
            message=resp_message,
            stop_reason="tool_use" if resp_message.tool_calls else "end_turn",
            usage=Usage(input_tokens=10, output_tokens=3),
        )
    )
    return p


async def test_successful_turn_persists_assistant_and_tool_results_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Happy path: both rows committed in one transaction."""
    cfg = _config(tmp_path)
    cfg.session.db_path.parent.mkdir(parents=True, exist_ok=True)

    # First call returns tool-use; second call returns final text (ends loop).
    first = _assistant_with_tool_call("fast_tool")
    second = _plain_assistant()
    provider = MagicMock()
    provider.complete = AsyncMock(
        side_effect=[
            ProviderResponse(message=first, stop_reason="tool_use", usage=Usage(10, 3)),
            ProviderResponse(message=second, stop_reason="end_turn", usage=Usage(20, 5)),
        ]
    )

    async def fake_dispatch(call: ToolCall, **_kwargs) -> ToolResult:
        return ToolResult(tool_call_id=call.id, content="tool output")

    # Patch the registry dispatch so we don't need real tools registered.
    from opencomputer.tools.registry import registry

    monkeypatch.setattr(registry, "dispatch", AsyncMock(side_effect=fake_dispatch))
    monkeypatch.setattr(registry, "schemas", MagicMock(return_value=[]))
    monkeypatch.setattr(
        registry,
        "get",
        MagicMock(return_value=MagicMock(parallel_safe=False)),
    )

    loop = AgentLoop(provider=provider, config=cfg, compaction_disabled=True)
    result = await loop.run_conversation(user_message="hi", session_id="s-happy")

    rows = loop.db.get_messages("s-happy")
    # user + assistant(tool_call) + tool_result + assistant(final)
    assert [m.role for m in rows] == ["user", "assistant", "tool", "assistant"]
    # The assistant-with-tool_calls must be followed immediately by its tool result
    assert rows[1].tool_calls is not None and rows[1].tool_calls[0].id == "tc-1"
    assert rows[2].tool_call_id == "tc-1"
    assert result.iterations == 2


async def test_cancel_mid_dispatch_leaves_no_dangling_assistant_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If dispatch is cancelled, neither the assistant message nor its
    (non-existent) tool_results end up in the DB — no dangling tool_use."""
    cfg = _config(tmp_path)
    cfg.session.db_path.parent.mkdir(parents=True, exist_ok=True)

    first = _assistant_with_tool_call("slow_tool")
    provider = _mock_provider(first)

    # Tool dispatch hangs until cancelled.
    async def never_ending(call: ToolCall, **_kwargs) -> ToolResult:
        await asyncio.sleep(10.0)
        return ToolResult(tool_call_id=call.id, content="should never reach")

    from opencomputer.tools.registry import registry

    monkeypatch.setattr(registry, "dispatch", AsyncMock(side_effect=never_ending))
    monkeypatch.setattr(registry, "schemas", MagicMock(return_value=[]))
    monkeypatch.setattr(
        registry,
        "get",
        MagicMock(return_value=MagicMock(parallel_safe=False)),
    )

    loop = AgentLoop(provider=provider, config=cfg, compaction_disabled=True)
    task = asyncio.create_task(
        loop.run_conversation(user_message="hi", session_id="s-cancel")
    )
    # Give it enough time to reach the dispatch await.
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    rows = loop.db.get_messages("s-cancel")
    # Only the user message should be persisted. Crucially, NO assistant row
    # with tool_calls but no matching tool row.
    assert [m.role for m in rows] == ["user"]
    # Sanity: the invariant Anthropic cares about — every assistant row with
    # tool_calls must have all of its tool_result rows present.
    for m in rows:
        if m.role == "assistant" and m.tool_calls:
            expected_ids = {tc.id for tc in m.tool_calls}
            seen_ids = {r.tool_call_id for r in rows if r.role == "tool"}
            assert expected_ids <= seen_ids, (
                "assistant row left dangling without its tool_results"
            )


def test_batch_append_is_atomic(tmp_path: Path) -> None:
    """SessionDB.append_messages_batch writes all rows in a single transaction."""
    db = SessionDB(tmp_path / "batch.db")
    db.create_session("s-batch", platform="test", model="m")
    msgs = [
        Message(
            role="assistant",
            content="calling tool",
            tool_calls=[ToolCall(id="a", name="t", arguments={})],
        ),
        Message(role="tool", content="result", tool_call_id="a", name="t"),
    ]
    ids = db.append_messages_batch("s-batch", msgs)
    assert len(ids) == 2
    rows = db.get_messages("s-batch")
    assert [m.role for m in rows] == ["assistant", "tool"]
    assert rows[0].tool_calls and rows[0].tool_calls[0].id == "a"
    assert rows[1].tool_call_id == "a"

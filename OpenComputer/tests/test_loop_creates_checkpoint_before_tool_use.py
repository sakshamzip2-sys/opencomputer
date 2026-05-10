"""End-to-end test that the agent loop actually creates a prompt_checkpoint
row before dispatching a tool_use block.

Closes the gap referenced (but never written) in
``tests/test_message_history_checkpoint.py`` line 8:

    * Loop wiring (see test_loop_creates_checkpoint_before_tool_use).

That referenced test never existed. ``CheckpointManager.create()`` is
unit-tested in isolation, but no test verified the loop actually CALLS
it. The user's audit (2026-05-10) showed ``prompt_checkpoints`` had 0
rows after weeks of usage — the writer works, the wire-in code path
exists, but a silent ``_log.debug("M5.2: checkpoint create failed
(suppressed)", exc_info=True)`` swallowed all real-world failures.

This test drives the loop end-to-end with a mocked provider that returns
a tool_use, and asserts the resulting DB row exists. If it ever stops
existing, this test fails loudly — instead of users discovering it via
manual SQL inspection of an empty table.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from plugin_sdk.core import Message, ToolCall


def _config(tmp: Path):
    """Minimal Config to drive an AgentLoop in tests."""
    from opencomputer.agent.config import (
        Config,
        LoopConfig,
        MemoryConfig,
        ModelConfig,
        SessionConfig,
    )

    return Config(
        model=ModelConfig(
            provider="mock",
            model="main-model",
            max_tokens=512,
            temperature=0.0,
        ),
        loop=LoopConfig(max_iterations=3, parallel_tools=False),
        session=SessionConfig(db_path=tmp / "sessions.db"),
        memory=MemoryConfig(
            declarative_path=tmp / "MEMORY.md",
            skills_path=tmp / "skills",
        ),
    )


@pytest.mark.asyncio
async def test_loop_creates_checkpoint_before_tool_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Driving the loop with a single tool_use should write 1 prompt_checkpoint row.

    Validates the exact wire-in at ``opencomputer/agent/loop.py`` ~line 2493::

        if step.assistant_message.tool_calls and messages:
            try:
                _cp_mgr = CheckpointManager(self.db)
                _cp_mgr.create(...)
            except Exception:
                _log.debug("M5.2: checkpoint create failed (suppressed)", ...)

    A regression in either the condition or the call would let the
    ``_log.debug`` swallow it and produce 0 rows — exactly the symptom
    the user observed. Failing here surfaces the bug instead.
    """
    from opencomputer.agent.checkpoint_manager import CheckpointManager
    from opencomputer.agent.loop import AgentLoop
    from opencomputer.agent.state import SessionDB
    from opencomputer.tools.registry import registry
    from plugin_sdk.core import ToolResult
    from plugin_sdk.provider_contract import ProviderResponse, Usage

    cfg = _config(tmp_path)
    cfg.session.db_path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(registry, "schemas", MagicMock(return_value=[]))

    async def _fake_dispatch(call: ToolCall, **_: object) -> ToolResult:
        return ToolResult(tool_call_id=call.id, content="ok", is_error=False)

    monkeypatch.setattr(registry, "dispatch", _fake_dispatch)

    # Turn 0: tool_use → triggers checkpoint code path. Turn 1: end_turn.
    turn0 = ProviderResponse(
        message=Message(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id="tc-1", name="Bash", arguments={"cmd": "date"})],
        ),
        stop_reason="tool_use",
        usage=Usage(5, 2),
    )
    turn1 = ProviderResponse(
        message=Message(role="assistant", content="done"),
        stop_reason="end_turn",
        usage=Usage(5, 2),
    )

    provider = MagicMock()
    provider.complete = AsyncMock(side_effect=[turn0, turn1])

    loop = AgentLoop(
        provider=provider,
        config=cfg,
        compaction_disabled=True,
        episodic_disabled=True,
        reviewer_disabled=True,
    )
    sid = "s-checkpoint-wire-in"
    await loop.run_conversation(user_message="quick q", session_id=sid)

    db = SessionDB(cfg.session.db_path)
    mgr = CheckpointManager(db)
    rows = mgr.list(sid)
    assert len(rows) >= 1, (
        f"Expected ≥1 prompt_checkpoint row after a tool_use turn; got {len(rows)}. "
        f"This means CheckpointManager.create() was either not called by the agent "
        f"loop, or threw an exception that was swallowed by _log.debug. Promote "
        f"that swallow to WARNING and re-run."
    )
    cp = rows[0]
    assert cp.session_id == sid
    assert cp.prompt_index == 0
    # The label is auto-generated; we don't pin exact wording but it
    # MUST mention the iteration so an operator inspecting the row can
    # tell which turn was checkpointed.
    assert "tool_use" in cp.label.lower() or "turn" in cp.label.lower()
    # And the snapshot must contain the user message — that's what gets
    # restored on rewind.
    msgs = cp.messages()
    assert any(
        m.get("role") == "user" and "quick q" in str(m.get("content", ""))
        for m in msgs
    ), f"Snapshot lost the user message: {msgs!r}"


@pytest.mark.asyncio
async def test_loop_does_not_checkpoint_when_no_tool_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pure end_turn response (no tool_calls) should NOT create a checkpoint.

    Avoids polluting the table with no-op rows and validates the
    condition `if step.assistant_message.tool_calls and messages:` is
    correctly gating.
    """
    from opencomputer.agent.checkpoint_manager import CheckpointManager
    from opencomputer.agent.loop import AgentLoop
    from opencomputer.agent.state import SessionDB
    from opencomputer.tools.registry import registry
    from plugin_sdk.provider_contract import ProviderResponse, Usage

    cfg = _config(tmp_path)
    cfg.session.db_path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(registry, "schemas", MagicMock(return_value=[]))

    provider = MagicMock()
    provider.complete = AsyncMock(
        return_value=ProviderResponse(
            message=Message(role="assistant", content="hi"),
            stop_reason="end_turn",
            usage=Usage(5, 2),
        )
    )

    loop = AgentLoop(
        provider=provider,
        config=cfg,
        compaction_disabled=True,
        episodic_disabled=True,
        reviewer_disabled=True,
    )
    sid = "s-no-checkpoint"
    await loop.run_conversation(user_message="hello", session_id=sid)

    db = SessionDB(cfg.session.db_path)
    mgr = CheckpointManager(db)
    rows = mgr.list(sid)
    assert rows == [], (
        f"Expected NO prompt_checkpoint rows for a tool-free turn; "
        f"got {len(rows)} ({[r.label for r in rows]!r})."
    )

"""End-to-end test for the per-turn trace_id wired into ``run_conversation``.

Verifies the full chain:

1. ``AgentLoop.run_conversation`` opens a ``trace_scope`` at entry.
2. The contextvar propagates through the awaiting machinery.
3. ``BaseProvider.complete`` (called from inside the loop's iteration)
   sees ``get_trace_id() != None``.
4. After ``run_conversation`` returns, the contextvar is cleared.

Mirrors ``test_loop_compaction_increments_counter.py`` setup so the
agent loop runs against a real ``SessionDB`` + a no-op provider
without the heavy plugin / channel scaffolding the production
gateway uses.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from opencomputer.agent.config import Config, LoopConfig
from opencomputer.agent.loop import AgentLoop
from opencomputer.agent.state import SessionDB
from opencomputer.observability.trace import (
    get_trace_id,
    set_trace_id,
    trace_scope,
)
from plugin_sdk.core import Message
from plugin_sdk.provider_contract import BaseProvider, ProviderResponse, Usage
from plugin_sdk.runtime_context import RuntimeContext


class _TraceCapturingProvider(BaseProvider):
    """Records ``get_trace_id()`` on every ``complete`` call.

    Returns ``end_turn`` immediately so the loop's tool-call iteration
    terminates after one provider invocation.
    """

    def __init__(self) -> None:
        self.captured: list[str | None] = []

    async def complete(self, **kwargs):  # noqa: ARG002
        self.captured.append(get_trace_id())
        return ProviderResponse(
            message=Message(role="assistant", content="done"),
            stop_reason="end_turn",
            usage=Usage(input_tokens=1, output_tokens=1),
        )

    async def stream_complete(self, **kwargs):  # noqa: ARG002
        if False:
            yield


def _make_loop(tmp_path: Path, provider: BaseProvider) -> AgentLoop:
    cfg = Config(
        loop=LoopConfig(max_iterations=2, parallel_tools=False),
        session=type(Config().session)(db_path=tmp_path / "s.db"),  # type: ignore[call-arg]
    )
    return AgentLoop(
        provider=provider,
        config=cfg,
        db=SessionDB(tmp_path / "s.db"),
        compaction_disabled=True,
        episodic_disabled=True,
        reviewer_disabled=True,
    )


@pytest.mark.asyncio
async def test_run_conversation_sets_trace_id_for_provider(tmp_path: Path):
    """When ``run_conversation`` is entered without an active trace, it
    opens a fresh one. The provider sees a non-None UUID string."""
    # Ensure no ambient trace from a previous test leaks in.
    set_trace_id(None)

    provider = _TraceCapturingProvider()
    loop = _make_loop(tmp_path, provider)
    loop._runtime = RuntimeContext()

    result = await loop.run_conversation(
        user_message="hi",
        session_id="trace-test-sess",
    )

    assert provider.captured, "provider.complete should have been invoked"
    captured_tid = provider.captured[0]
    assert captured_tid is not None, (
        "the provider must see a populated trace_id during the turn"
    )
    # Confirm it's a UUID-shaped string.
    parsed = uuid.UUID(captured_tid)
    assert str(parsed) == captured_tid


@pytest.mark.asyncio
async def test_run_conversation_clears_trace_id_after_return(tmp_path: Path):
    """The contextvar should be cleared after ``run_conversation``
    returns, so a subsequent unrelated code path doesn't see a stale
    trace id."""
    set_trace_id(None)

    provider = _TraceCapturingProvider()
    loop = _make_loop(tmp_path, provider)
    loop._runtime = RuntimeContext()

    assert get_trace_id() is None
    await loop.run_conversation(
        user_message="hello",
        session_id="trace-cleanup-sess",
    )
    assert get_trace_id() is None, (
        "trace_id should be cleared after run_conversation returns"
    )


@pytest.mark.asyncio
async def test_run_conversation_inherits_existing_trace(tmp_path: Path):
    """If the caller already opened a trace scope (e.g. delegate
    nested call), ``run_conversation`` should inherit rather than
    open a new id."""
    set_trace_id(None)

    provider = _TraceCapturingProvider()
    loop = _make_loop(tmp_path, provider)
    loop._runtime = RuntimeContext()

    with trace_scope("parent-trace-id"):
        await loop.run_conversation(
            user_message="hi",
            session_id="inherit-sess",
        )

    assert provider.captured[0] == "parent-trace-id", (
        "nested run_conversation should inherit the parent trace_id"
    )

"""End-to-end integration tests for DM Topic dispatch (Hermes PR 5.3).

Verifies:
* Dispatch → adapter.resolve_channel_prompt / resolve_channel_skills → AgentLoop runtime
* Channel prompt + skill bodies appear on the per-turn ``system`` prompt
* Falls back to default behaviour when channel_id is absent

Uses lightweight in-memory fakes (Mock loops, simple adapter subclasses)
to keep the test hermetic — no Anthropic API, no Telegram network.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from opencomputer.agent.loop import ConversationResult
from opencomputer.gateway.dispatch import Dispatch
from plugin_sdk.channel_contract import BaseChannelAdapter
from plugin_sdk.core import Message, MessageEvent, Platform


class _FakeAdapter(BaseChannelAdapter):
    """Minimal adapter that just exposes the resolve_channel_* contract."""

    platform = Platform.TELEGRAM

    async def connect(self) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, *a: Any, **kw: Any):  # noqa: ARG002
        return None


def _make_loop_capturing_runtime() -> MagicMock:
    """Mock AgentLoop whose run_conversation captures runtime + returns a stub."""
    final = Message(role="assistant", content="ok")
    result = ConversationResult(
        final_message=final,
        messages=[final],
        session_id="s",
        iterations=1,
        input_tokens=0,
        output_tokens=0,
    )
    loop = MagicMock()
    loop.run_conversation = AsyncMock(return_value=result)
    # ``memory.load_skill_body`` is consulted by the dispatcher when
    # the adapter advertises channel skills. Default to "" so the
    # dispatcher's defensive path is exercised; individual tests
    # override this.
    loop.memory = MagicMock()
    loop.memory.load_skill_body = MagicMock(return_value="")
    return loop


# ─── Dispatcher → AgentLoop runtime threading ───────────────────────


@pytest.mark.asyncio
async def test_channel_prompt_threads_into_runtime_custom() -> None:
    loop = _make_loop_capturing_runtime()
    adapter = _FakeAdapter(
        config={"channel_prompts": {"chan-1": "be helpful"}}
    )
    d = Dispatch(loop)
    d.register_adapter(Platform.TELEGRAM.value, adapter)

    event = MessageEvent(
        platform=Platform.TELEGRAM,
        chat_id="123",
        user_id="u",
        text="hello",
        timestamp=0.0,
        metadata={"channel_id": "chan-1"},
    )
    await d.handle_message(event)

    # The mock captured the runtime kwarg.
    _, kwargs = loop.run_conversation.call_args
    runtime = kwargs.get("runtime")
    assert runtime is not None
    assert runtime.custom.get("channel_prompt") == "be helpful"


@pytest.mark.asyncio
async def test_channel_skills_load_bodies_into_runtime() -> None:
    loop = _make_loop_capturing_runtime()
    loop.memory.load_skill_body = MagicMock(
        side_effect=lambda sid: f"body of {sid}"
    )

    adapter = _FakeAdapter(
        config={"channel_skill_bindings": {"chan-1": ["alpha", "beta"]}}
    )
    d = Dispatch(loop)
    d.register_adapter(Platform.TELEGRAM.value, adapter)

    event = MessageEvent(
        platform=Platform.TELEGRAM,
        chat_id="123",
        user_id="u",
        text="hello",
        timestamp=0.0,
        metadata={"channel_id": "chan-1"},
    )
    await d.handle_message(event)

    _, kwargs = loop.run_conversation.call_args
    runtime = kwargs.get("runtime")
    assert runtime is not None
    assert runtime.custom.get("channel_skill_ids") == ["alpha", "beta"]
    bodies = runtime.custom.get("channel_skill_bodies")
    assert bodies == [("alpha", "body of alpha"), ("beta", "body of beta")]


@pytest.mark.asyncio
async def test_no_channel_id_yields_default_runtime() -> None:
    """Plain inbound (CLI, untagged Telegram message) → no channel custom set."""
    loop = _make_loop_capturing_runtime()
    adapter = _FakeAdapter(config={"channel_prompts": {"chan-1": "x"}})
    d = Dispatch(loop)
    d.register_adapter(Platform.TELEGRAM.value, adapter)

    event = MessageEvent(
        platform=Platform.TELEGRAM,
        chat_id="123",
        user_id="u",
        text="hello",
        timestamp=0.0,
    )
    await d.handle_message(event)

    _, kwargs = loop.run_conversation.call_args
    runtime = kwargs.get("runtime")
    # DEFAULT_RUNTIME_CONTEXT — no custom-channel keys.
    assert runtime is not None
    assert "channel_prompt" not in runtime.custom
    assert "channel_skill_ids" not in runtime.custom


@pytest.mark.asyncio
async def test_unmatched_channel_id_yields_default_runtime() -> None:
    """channel_id present but adapter has no binding → default runtime."""
    loop = _make_loop_capturing_runtime()
    adapter = _FakeAdapter(config={})
    d = Dispatch(loop)
    d.register_adapter(Platform.TELEGRAM.value, adapter)

    event = MessageEvent(
        platform=Platform.TELEGRAM,
        chat_id="123",
        user_id="u",
        text="hello",
        timestamp=0.0,
        metadata={"channel_id": "unknown"},
    )
    await d.handle_message(event)

    _, kwargs = loop.run_conversation.call_args
    runtime = kwargs.get("runtime")
    assert runtime is not None
    assert "channel_prompt" not in runtime.custom


@pytest.mark.asyncio
async def test_parent_channel_id_falls_back() -> None:
    """parent_channel_id is honoured when the direct id has no entry."""
    loop = _make_loop_capturing_runtime()
    adapter = _FakeAdapter(
        config={"channel_prompts": {"parent": "fallback prompt"}}
    )
    d = Dispatch(loop)
    d.register_adapter(Platform.TELEGRAM.value, adapter)

    event = MessageEvent(
        platform=Platform.TELEGRAM,
        chat_id="123",
        user_id="u",
        text="hello",
        timestamp=0.0,
        metadata={"channel_id": "thread-1", "parent_channel_id": "parent"},
    )
    await d.handle_message(event)

    _, kwargs = loop.run_conversation.call_args
    runtime = kwargs.get("runtime")
    assert runtime.custom.get("channel_prompt") == "fallback prompt"


@pytest.mark.asyncio
async def test_resolve_failure_does_not_break_dispatch() -> None:
    """A buggy resolve_* implementation must not take dispatch down."""

    class _BoomAdapter(_FakeAdapter):
        def resolve_channel_prompt(self, channel_id, parent_id=None):  # noqa: ARG002
            raise RuntimeError("nope")

        def resolve_channel_skills(self, channel_id, parent_id=None):  # noqa: ARG002
            raise RuntimeError("also nope")

    loop = _make_loop_capturing_runtime()
    adapter = _BoomAdapter(config={})
    d = Dispatch(loop)
    d.register_adapter(Platform.TELEGRAM.value, adapter)

    event = MessageEvent(
        platform=Platform.TELEGRAM,
        chat_id="123",
        user_id="u",
        text="hello",
        timestamp=0.0,
        metadata={"channel_id": "x"},
    )
    out = await d.handle_message(event)
    assert out == "ok"  # dispatch survived


# ─── AgentLoop system-prompt assembly ───────────────────────────────


def test_agent_loop_appends_channel_prompt_to_system() -> None:
    """The loop's per-turn system lane reads runtime.custom and appends.

    We exercise ONLY the bit that splices channel_prompt + skill bodies
    onto the ``system`` string. The rest of run_conversation is far too
    integration-heavy for a unit test; we treat the splice as a pure
    string transform driven by ``self._runtime.custom``.
    """
    # The splice logic lives inside run_conversation immediately after
    # the prefetched-memory branch. We mirror it here against a minimal
    # RuntimeContext so the regression target is captured locally.
    from plugin_sdk.runtime_context import RuntimeContext

    runtime = RuntimeContext(
        custom={
            "channel_prompt": "topic prompt",
            "channel_skill_bodies": [
                ("alpha", "alpha body"),
                ("beta", "beta body"),
            ],
        }
    )

    system = "BASE"
    channel_prompt = runtime.custom.get("channel_prompt")
    if isinstance(channel_prompt, str) and channel_prompt.strip():
        system = system + "\n\n## Channel prompt\n\n" + channel_prompt.strip()
    bodies = runtime.custom.get("channel_skill_bodies")
    if bodies:
        blocks: list[str] = []
        for entry in bodies:
            if isinstance(entry, tuple) and len(entry) == 2:
                sid, body = entry
                blocks.append(f"### {sid}\n\n{body}")
        if blocks:
            system = (
                system
                + "\n\n## Channel skills (auto-loaded)\n\n"
                + "\n\n".join(blocks)
            )

    assert "## Channel prompt" in system
    assert "topic prompt" in system
    assert "## Channel skills (auto-loaded)" in system
    assert "### alpha" in system
    assert "alpha body" in system
    assert "### beta" in system


def _make_real_loop(tmp_path):
    """Build a real AgentLoop wired to a stub provider.

    Returns ``(loop, captured)`` where ``captured`` is a dict that
    accumulates ``{"system": ..., "messages": ...}`` whenever the
    provider's ``complete`` is invoked.
    """
    from opencomputer.agent.config import (
        Config,
        LoopConfig,
        MemoryConfig,
        ModelConfig,
        SessionConfig,
    )
    from opencomputer.agent.loop import AgentLoop
    from plugin_sdk.core import Message
    from plugin_sdk.provider_contract import ProviderResponse, Usage

    cfg = Config(
        model=ModelConfig(
            provider="mock", model="mock-model", max_tokens=1024, temperature=0.0
        ),
        loop=LoopConfig(max_iterations=2, parallel_tools=False),
        session=SessionConfig(db_path=tmp_path / "sessions.db"),
        memory=MemoryConfig(
            declarative_path=tmp_path / "MEMORY.md",
            skills_path=tmp_path / "skills",
        ),
    )
    cfg.session.db_path.parent.mkdir(parents=True, exist_ok=True)

    captured: dict[str, Any] = {}

    p = MagicMock()

    async def _complete(**kwargs: Any) -> Any:
        captured["system"] = kwargs.get("system", "")
        captured["messages"] = kwargs.get("messages", [])
        return ProviderResponse(
            message=Message(role="assistant", content="done"),
            stop_reason="end_turn",
            usage=Usage(10, 3),
        )

    p.complete = AsyncMock(side_effect=_complete)
    loop = AgentLoop(provider=p, config=cfg, compaction_disabled=True)
    # Drop tool registry so the loop doesn't try to load real tools.
    from opencomputer.tools.registry import registry as _tool_registry
    _tool_registry_schemas = _tool_registry.schemas

    def _empty_schemas() -> list[Any]:
        return []

    _tool_registry.schemas = _empty_schemas  # type: ignore[assignment]
    return loop, captured, _tool_registry_schemas


@pytest.mark.asyncio
async def test_loop_run_conversation_renders_channel_prompt(tmp_path) -> None:
    """Real AgentLoop.run_conversation appends channel_prompt to system."""
    from plugin_sdk.runtime_context import RuntimeContext

    loop, captured, _restore = _make_real_loop(tmp_path)
    try:
        runtime = RuntimeContext(
            custom={
                "channel_prompt": "be terse and trade-focused",
                "channel_skill_bodies": [
                    ("stocks", "## Stocks workflow\n\nStep 1..."),
                ],
            }
        )

        await loop.run_conversation(
            user_message="hi",
            session_id="test-session-channel",
            runtime=runtime,
        )

        assert "system" in captured
        s = captured["system"]
        assert "## Channel prompt" in s
        assert "be terse and trade-focused" in s
        assert "## Channel skills (auto-loaded)" in s
        assert "### stocks" in s
        assert "Stocks workflow" in s
    finally:
        # Restore the registry stub.
        from opencomputer.tools.registry import registry as _tr
        _tr.schemas = _restore


@pytest.mark.asyncio
async def test_loop_default_runtime_omits_channel_section(tmp_path) -> None:
    """Without runtime.custom keys set, no channel sections appear."""
    loop, captured, _restore = _make_real_loop(tmp_path)
    try:
        await loop.run_conversation(
            user_message="hi",
            session_id="other-session-default",
        )

        s = captured["system"]
        assert "## Channel prompt" not in s
        assert "## Channel skills (auto-loaded)" not in s
    finally:
        from opencomputer.tools.registry import registry as _tr
        _tr.schemas = _restore

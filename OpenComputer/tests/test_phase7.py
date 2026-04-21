"""Phase 7 tests: streaming providers, CLI streaming plumbing, typing heartbeat."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from plugin_sdk.core import Message, Platform
from plugin_sdk.provider_contract import ProviderResponse, StreamEvent, Usage

# ─── StreamEvent dataclass ─────────────────────────────────────


def test_stream_event_shapes() -> None:
    e = StreamEvent(kind="text_delta", text="hello")
    assert e.kind == "text_delta"
    assert e.text == "hello"
    assert e.response is None

    resp = ProviderResponse(
        message=Message(role="assistant", content="full"),
        stop_reason="end_turn",
        usage=Usage(input_tokens=10, output_tokens=5),
    )
    e_done = StreamEvent(kind="done", response=resp)
    assert e_done.kind == "done"
    assert e_done.response is resp


# ─── AgentLoop streaming integration ───────────────────────────


def test_agent_loop_calls_stream_callback_for_text_deltas() -> None:
    """When stream_callback is provided, loop.stream_complete is used and
    each text_delta triggers the callback."""
    from opencomputer.agent.config import default_config
    from opencomputer.agent.loop import AgentLoop

    # Build a mock provider whose stream_complete yields 2 deltas + done
    final = ProviderResponse(
        message=Message(role="assistant", content="hello world"),
        stop_reason="end_turn",
        usage=Usage(input_tokens=20, output_tokens=2),
    )

    async def fake_stream(**kw):
        yield StreamEvent(kind="text_delta", text="hello ")
        yield StreamEvent(kind="text_delta", text="world")
        yield StreamEvent(kind="done", response=final)

    provider = MagicMock()
    provider.stream_complete = fake_stream

    loop = AgentLoop(provider=provider, config=default_config())

    chunks: list[str] = []
    async def run():
        await loop._run_one_step(
            messages=[Message(role="user", content="hi")],
            system="sys",
            stream_callback=lambda c: chunks.append(c),
        )

    asyncio.run(run())
    assert chunks == ["hello ", "world"]


def test_agent_loop_without_stream_callback_uses_complete() -> None:
    """No callback → provider.complete() is called, stream_complete is NOT."""
    from opencomputer.agent.config import default_config
    from opencomputer.agent.loop import AgentLoop

    final = ProviderResponse(
        message=Message(role="assistant", content="ok"),
        stop_reason="end_turn",
        usage=Usage(input_tokens=5, output_tokens=1),
    )
    provider = MagicMock()
    provider.complete = AsyncMock(return_value=final)
    provider.stream_complete = MagicMock()  # if accessed it'd fail async usage

    loop = AgentLoop(provider=provider, config=default_config())

    async def run():
        return await loop._run_one_step(
            messages=[Message(role="user", content="hi")], system="sys"
        )

    outcome = asyncio.run(run())
    assert outcome.assistant_message.content == "ok"
    provider.complete.assert_awaited_once()
    # stream_complete must NOT have been iterated
    assert provider.stream_complete.call_count == 0


def test_agent_loop_raises_if_stream_never_done() -> None:
    """Defensive: if the provider's stream ends without 'done', we raise
    instead of silently returning garbage."""
    from opencomputer.agent.config import default_config
    from opencomputer.agent.loop import AgentLoop

    async def bad_stream(**kw):
        yield StreamEvent(kind="text_delta", text="hi")
        # no 'done' event

    provider = MagicMock()
    provider.stream_complete = bad_stream

    loop = AgentLoop(provider=provider, config=default_config())

    async def run():
        await loop._run_one_step(
            messages=[Message(role="user", content="x")],
            system="",
            stream_callback=lambda c: None,
        )

    with pytest.raises(RuntimeError, match="stream ended without"):
        asyncio.run(run())


# ─── Typing heartbeat ──────────────────────────────────────────


def test_typing_heartbeat_sends_repeatedly_while_turn_in_flight() -> None:
    """Heartbeat calls adapter.send_typing periodically until cancelled."""
    from opencomputer.gateway.dispatch import Dispatch

    adapter = MagicMock()
    adapter.send_typing = AsyncMock()

    loop_mock = MagicMock()
    d = Dispatch(loop_mock)
    d.register_adapter(Platform.TELEGRAM.value, adapter)

    async def run_briefly():
        task = asyncio.create_task(
            d._typing_heartbeat(Platform.TELEGRAM.value, "chat123")
        )
        # Let it loop a bit. Default interval is 4s in prod — we check the
        # first beat fires immediately.
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run_briefly())
    # At least one typing call happened
    assert adapter.send_typing.await_count >= 1
    adapter.send_typing.assert_awaited_with("chat123")


def test_typing_heartbeat_noop_if_adapter_unknown() -> None:
    """Unknown platform → silent no-op (no crash)."""
    from opencomputer.gateway.dispatch import Dispatch

    d = Dispatch(MagicMock())

    async def run():
        await d._typing_heartbeat("nonexistent-platform", "anyone")

    # Must not raise
    asyncio.run(run())


def test_dispatch_registers_adapters_by_platform() -> None:
    from opencomputer.gateway.server import Gateway

    adapter = MagicMock()
    adapter.platform = Platform.TELEGRAM

    gw = Gateway(loop=MagicMock())
    gw.register_adapter(adapter)
    # Gateway should have wired the adapter into the dispatch too
    assert gw.dispatch._adapters_by_platform.get("telegram") is adapter


def test_typing_heartbeat_handles_adapter_exceptions() -> None:
    """If adapter.send_typing raises, the heartbeat swallows it and keeps going."""
    from opencomputer.gateway.dispatch import Dispatch

    adapter = MagicMock()
    adapter.send_typing = AsyncMock(side_effect=RuntimeError("network flap"))

    d = Dispatch(MagicMock())
    d.register_adapter("telegram", adapter)

    async def run_briefly():
        task = asyncio.create_task(d._typing_heartbeat("telegram", "cid"))
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # Must complete without exception propagating out
    asyncio.run(run_briefly())


# ─── OpenAI stream aggregation (text only, no network) ─────────


def test_openai_provider_module_loads() -> None:
    """Verify the OpenAI provider still imports after the stream changes."""
    import importlib.util
    from pathlib import Path

    repo_root = Path(__file__).resolve().parent.parent
    path = repo_root / "extensions" / "openai-provider" / "provider.py"
    spec = importlib.util.spec_from_file_location("p7_openai_prov", path)
    assert spec is not None and spec.loader is not None
    import sys

    mod = importlib.util.module_from_spec(spec)
    sys.modules["p7_openai_prov"] = mod
    spec.loader.exec_module(mod)
    assert hasattr(mod, "OpenAIProvider")


def test_anthropic_provider_module_loads() -> None:
    """Verify the Anthropic provider still imports after the stream changes."""
    import importlib.util
    import sys
    from pathlib import Path

    repo_root = Path(__file__).resolve().parent.parent
    path = repo_root / "extensions" / "anthropic-provider" / "provider.py"
    spec = importlib.util.spec_from_file_location("p7_anthropic_prov", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["p7_anthropic_prov"] = mod
    spec.loader.exec_module(mod)
    assert hasattr(mod, "AnthropicProvider")

"""Round 2a P-2 — ``/steer`` mid-run nudges.

Coverage:
  * SteerRegistry: submit + consume round-trip, latest-wins override,
    has_pending peek, clear, thread safety under contention.
  * AgentLoop integration: a nudge submitted between iterations lands
    as a synthetic user message in the next ``_run_one_step`` call.
  * Wire server: ``steer.submit`` JSON-RPC method validates inputs,
    ack carries ``had_pending``, the registry receives the nudge.
  * Telegram adapter: ``/steer <text>`` is intercepted before reaching
    the gateway dispatcher; usage hint on empty body; latest-wins
    surfaces in the ack message.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import threading
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import websockets

# ─── SteerRegistry unit tests ──────────────────────────────────────


def test_submit_then_consume_returns_nudge() -> None:
    from opencomputer.agent.steer import SteerRegistry

    reg = SteerRegistry()
    reg.submit("sess-1", "focus on the build error")
    assert reg.has_pending("sess-1") is True
    assert reg.consume("sess-1") == "focus on the build error"
    # consume clears
    assert reg.has_pending("sess-1") is False
    assert reg.consume("sess-1") is None


def test_consume_returns_none_when_no_nudge() -> None:
    from opencomputer.agent.steer import SteerRegistry

    reg = SteerRegistry()
    assert reg.consume("never-touched") is None
    assert reg.has_pending("never-touched") is False


def test_latest_wins_override_logs_warning(caplog) -> None:
    from opencomputer.agent.steer import SteerRegistry

    reg = SteerRegistry()
    reg.submit("sess-1", "first")
    with caplog.at_level(logging.WARNING, logger="opencomputer.agent.steer"):
        reg.submit("sess-1", "second")
    # only the latest survives
    assert reg.consume("sess-1") == "second"
    # WARNING was logged about the override
    assert any(
        "steer override" in rec.getMessage().lower()
        and "sess-1" in rec.getMessage()
        for rec in caplog.records
    )


def test_per_session_isolation() -> None:
    from opencomputer.agent.steer import SteerRegistry

    reg = SteerRegistry()
    reg.submit("alice", "A nudge")
    reg.submit("bob", "B nudge")
    assert reg.consume("alice") == "A nudge"
    # bob's nudge is untouched by alice's consume
    assert reg.consume("bob") == "B nudge"


def test_submit_normalizes_whitespace_and_drops_empty() -> None:
    from opencomputer.agent.steer import SteerRegistry

    reg = SteerRegistry()
    reg.submit("s", "  hello  ")
    assert reg.consume("s") == "hello"
    # empty / whitespace-only body is dropped silently; no override
    reg.submit("s", "real nudge")
    reg.submit("s", "   ")  # no-op, doesn't override
    assert reg.consume("s") == "real nudge"


def test_clear_drops_without_consuming(caplog) -> None:
    from opencomputer.agent.steer import SteerRegistry

    reg = SteerRegistry()
    reg.submit("s", "x")
    reg.clear("s")
    assert reg.has_pending("s") is False


def test_submit_rejects_empty_session_id() -> None:
    from opencomputer.agent.steer import SteerRegistry

    reg = SteerRegistry()
    with pytest.raises(ValueError):
        reg.submit("", "x")


def test_thread_safe_concurrent_submits() -> None:
    """Many threads pounding the same key — registry must not crash and
    must end with one of the values, not partial state."""
    from opencomputer.agent.steer import SteerRegistry

    reg = SteerRegistry()
    n_threads = 32
    barrier = threading.Barrier(n_threads)

    def worker(i: int) -> None:
        barrier.wait()
        for j in range(50):
            reg.submit("contended", f"thread-{i}-iter-{j}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    final = reg.consume("contended")
    assert final is not None
    # The surviving value must be one of the legitimate writes — never
    # truncated, never None.
    assert final.startswith("thread-")


def test_format_nudge_message_produces_documented_shape() -> None:
    from opencomputer.agent.steer import format_nudge_message

    rendered = format_nudge_message("focus on tests")
    assert rendered.startswith("<USER-NUDGE>: focus on tests")
    assert "latest-wins" in rendered


# ─── AgentLoop integration ─────────────────────────────────────────


def _build_provider_with_tool_then_end():
    """Provider that calls a tool on the first turn then ends on the second.

    Lets us exercise the between-turn checkpoint: first iteration emits
    a tool_use, second iteration is the no-tool end. The steer nudge
    should land on the synthetic user message between them.
    """
    from plugin_sdk.core import Message, ToolCall
    from plugin_sdk.provider_contract import BaseProvider, ProviderResponse, Usage

    class _Provider(BaseProvider):
        def __init__(self) -> None:
            self.calls: list[list[Message]] = []
            self._iter = 0

        async def complete(self, *, model, messages, system, tools, max_tokens, temperature, runtime_extras=None):
            # Snapshot the message list as it appears at the wire boundary
            # — this is what the test inspects to confirm the nudge landed.
            self.calls.append(list(messages))
            self._iter += 1
            if self._iter == 1:
                return ProviderResponse(
                    message=Message(
                        role="assistant",
                        content="",
                        tool_calls=[
                            ToolCall(id="t1", name="Ping", arguments={}),
                        ],
                    ),
                    stop_reason="tool_use",
                    usage=Usage(input_tokens=5, output_tokens=2),
                )
            return ProviderResponse(
                message=Message(role="assistant", content="all done"),
                stop_reason="end_turn",
                usage=Usage(input_tokens=5, output_tokens=2),
            )

        async def stream_complete(self, *, model, messages, system, tools, max_tokens, temperature):
            raise NotImplementedError

    return _Provider()


def _make_loop_with_provider(tmp_path, provider, registry):
    from opencomputer.agent.config import Config, LoopConfig
    from opencomputer.agent.loop import AgentLoop
    from opencomputer.agent.state import SessionDB

    cfg = Config(
        loop=LoopConfig(max_iterations=4, parallel_tools=False),
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


def test_agent_loop_consumes_nudge_between_turns(tmp_path, monkeypatch) -> None:
    """The synthetic ``<USER-NUDGE>`` message reaches the next LLM call."""
    from opencomputer.agent import steer as steer_mod
    from opencomputer.agent.steer import SteerRegistry
    from opencomputer.tools.registry import ToolRegistry
    from plugin_sdk.core import ToolCall, ToolResult
    from plugin_sdk.tool_contract import BaseTool, ToolSchema

    # Fresh registry — must replace the module-level singleton AgentLoop reads.
    fresh = SteerRegistry()
    monkeypatch.setattr(steer_mod, "default_registry", fresh, raising=True)

    # Fresh tool registry with a no-op Ping tool.
    class _Ping(BaseTool):
        @property
        def schema(self) -> ToolSchema:
            return ToolSchema(
                name="Ping",
                description="test",
                parameters={"type": "object", "properties": {}, "required": []},
            )

        async def execute(self, call: ToolCall) -> ToolResult:
            return ToolResult(tool_call_id=call.id, content="pong")

    import opencomputer.agent.loop as loop_mod
    test_reg = ToolRegistry()
    test_reg.register(_Ping())
    monkeypatch.setattr(loop_mod, "registry", test_reg, raising=True)

    provider = _build_provider_with_tool_then_end()
    loop = _make_loop_with_provider(tmp_path, provider, test_reg)

    # Pre-submit the nudge for the session id we'll pass into run_conversation.
    fresh.submit("sess-AB", "switch to plan mode and stop using tools")

    asyncio.run(
        loop.run_conversation(
            user_message="hello",
            session_id="sess-AB",
        )
    )

    # Provider received TWO complete() calls (tool_use → end_turn).
    assert len(provider.calls) == 2

    # First call sees only the original user message — no nudge yet.
    first_call = provider.calls[0]
    assert any(m.role == "user" and "hello" in (m.content or "") for m in first_call)
    assert not any(
        m.role == "user" and "<USER-NUDGE>" in (m.content or "")
        for m in first_call
    )

    # Second call MUST contain the synthetic nudge as a user message.
    second_call = provider.calls[1]
    nudge_msgs = [
        m for m in second_call
        if m.role == "user" and "<USER-NUDGE>" in (m.content or "")
    ]
    assert len(nudge_msgs) == 1
    body = nudge_msgs[0].content or ""
    assert "switch to plan mode and stop using tools" in body
    assert "latest-wins" in body

    # And the registry has been consumed — second consume returns None.
    assert fresh.consume("sess-AB") is None


def test_agent_loop_no_nudge_on_first_iteration(tmp_path, monkeypatch) -> None:
    """If a nudge is pending but the first iteration is a no-tool end,
    it stays in the registry — there's no between-turn moment."""
    from opencomputer.agent import steer as steer_mod
    from opencomputer.agent.steer import SteerRegistry
    from opencomputer.tools.registry import ToolRegistry
    from plugin_sdk.core import Message
    from plugin_sdk.provider_contract import BaseProvider, ProviderResponse, Usage

    fresh = SteerRegistry()
    monkeypatch.setattr(steer_mod, "default_registry", fresh, raising=True)

    import opencomputer.agent.loop as loop_mod
    monkeypatch.setattr(loop_mod, "registry", ToolRegistry(), raising=True)

    class _End(BaseProvider):
        async def complete(self, *, model, messages, system, tools, max_tokens, temperature, runtime_extras=None):
            return ProviderResponse(
                message=Message(role="assistant", content="done"),
                stop_reason="end_turn",
                usage=Usage(input_tokens=1, output_tokens=1),
            )

        async def stream_complete(self, *, model, messages, system, tools, max_tokens, temperature):
            raise NotImplementedError

    loop = _make_loop_with_provider(tmp_path, _End(), ToolRegistry())
    fresh.submit("sess-X", "stranded nudge")

    asyncio.run(
        loop.run_conversation(user_message="hi", session_id="sess-X")
    )

    # Single iteration → no between-turn point → nudge survives.
    assert fresh.consume("sess-X") == "stranded nudge"


# ─── Wire server JSON-RPC steer.submit ─────────────────────────────


async def _find_free_port() -> int:
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _build_fake_loop_minimal():
    from opencomputer.agent.loop import ConversationResult
    from plugin_sdk.core import Message

    loop = MagicMock()
    final = Message(role="assistant", content="x")
    loop.run_conversation = AsyncMock(
        return_value=ConversationResult(
            final_message=final, messages=[final], session_id="x",
            iterations=1, input_tokens=1, output_tokens=1,
        )
    )
    return loop


def test_wire_steer_submit_records_nudge(monkeypatch) -> None:
    """``steer.submit`` writes into the registry and acks ``had_pending``."""
    from opencomputer.agent import steer as steer_mod
    from opencomputer.agent.steer import SteerRegistry
    from opencomputer.gateway import wire_server as ws_mod
    from opencomputer.gateway.protocol import METHOD_STEER_SUBMIT
    from opencomputer.gateway.wire_server import WireServer

    fresh = SteerRegistry()
    monkeypatch.setattr(steer_mod, "default_registry", fresh, raising=True)
    # The wire_server module imported the registry by name; rebind there too.
    monkeypatch.setattr(ws_mod, "_steer_registry", fresh, raising=True)

    async def run() -> None:
        port = await _find_free_port()
        server = WireServer(loop=_build_fake_loop_minimal(), port=port)
        await server.start()
        try:
            async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
                await ws.send(json.dumps({
                    "type": "req",
                    "id": "1",
                    "method": METHOD_STEER_SUBMIT,
                    "params": {"session_id": "sess-W", "prompt": "hello"},
                }))
                resp = json.loads(
                    await asyncio.wait_for(ws.recv(), timeout=2.0)
                )
                assert resp["ok"] is True
                assert resp["payload"]["session_id"] == "sess-W"
                assert resp["payload"]["had_pending"] is False
                assert resp["payload"]["queued_chars"] == len("hello")

                # Second submit overrides; had_pending should now be True.
                await ws.send(json.dumps({
                    "type": "req",
                    "id": "2",
                    "method": METHOD_STEER_SUBMIT,
                    "params": {"session_id": "sess-W", "prompt": "newer"},
                }))
                resp2 = json.loads(
                    await asyncio.wait_for(ws.recv(), timeout=2.0)
                )
                assert resp2["ok"] is True
                assert resp2["payload"]["had_pending"] is True
        finally:
            await server.stop()

    asyncio.run(run())

    # Final state in the registry: only the latest value survives.
    assert fresh.consume("sess-W") == "newer"


def test_wire_steer_submit_validates_inputs(monkeypatch) -> None:
    """Missing session_id / empty prompt → error response, no registry write."""
    from opencomputer.agent import steer as steer_mod
    from opencomputer.agent.steer import SteerRegistry
    from opencomputer.gateway import wire_server as ws_mod
    from opencomputer.gateway.protocol import METHOD_STEER_SUBMIT
    from opencomputer.gateway.wire_server import WireServer

    fresh = SteerRegistry()
    monkeypatch.setattr(steer_mod, "default_registry", fresh, raising=True)
    monkeypatch.setattr(ws_mod, "_steer_registry", fresh, raising=True)

    async def run() -> None:
        port = await _find_free_port()
        server = WireServer(loop=_build_fake_loop_minimal(), port=port)
        await server.start()
        try:
            async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
                # Missing session_id
                await ws.send(json.dumps({
                    "type": "req",
                    "id": "1",
                    "method": METHOD_STEER_SUBMIT,
                    "params": {"prompt": "x"},
                }))
                resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
                assert resp["ok"] is False
                assert "session_id" in (resp["error"] or "").lower()

                # Empty prompt
                await ws.send(json.dumps({
                    "type": "req",
                    "id": "2",
                    "method": METHOD_STEER_SUBMIT,
                    "params": {"session_id": "s", "prompt": "   "},
                }))
                resp2 = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
                assert resp2["ok"] is False
                assert "prompt" in (resp2["error"] or "").lower()
        finally:
            await server.stop()

    asyncio.run(run())

    # Nothing landed in the registry.
    assert fresh.consume("s") is None


def test_wire_hello_advertises_steer_submit_method() -> None:
    """The hello handshake must list the new method so clients can discover it."""
    from opencomputer.gateway.protocol import METHOD_STEER_SUBMIT
    from opencomputer.gateway.wire_server import WireServer

    async def run() -> None:
        port = await _find_free_port()
        server = WireServer(loop=_build_fake_loop_minimal(), port=port)
        await server.start()
        try:
            async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
                await ws.send(json.dumps({
                    "type": "req", "id": "h", "method": "hello",
                }))
                resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
                assert resp["ok"] is True
                assert METHOD_STEER_SUBMIT in resp["payload"]["methods"]
        finally:
            await server.stop()

    asyncio.run(run())


# ─── Telegram adapter detection ────────────────────────────────────


# Load TelegramAdapter the same way other tests in this repo do —
# importlib loader avoids sibling-name collisions across plugin dirs.
_TELEGRAM_ADAPTER_PATH = (
    Path(__file__).resolve().parent.parent / "extensions" / "telegram" / "adapter.py"
)


def _load_telegram() -> Any:
    spec = importlib.util.spec_from_file_location(
        "telegram_adapter_test_steer",
        str(_TELEGRAM_ADAPTER_PATH),
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def telegram_adapter():
    """Telegram adapter wired against an httpx MockTransport.

    All ``sendMessage`` requests are captured on ``adapter._sent`` so
    tests can assert on the ack body the user would have received.
    """
    mod = _load_telegram()
    sent: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if "/getMe" in req.url.path:
            return httpx.Response(
                200,
                json={"ok": True, "result": {"id": 7, "username": "stbot"}},
            )
        if "/sendMessage" in req.url.path:
            try:
                body = json.loads(req.content)
            except Exception:
                body = {}
            sent.append(body)
            return httpx.Response(
                200, json={"ok": True, "result": {"message_id": 1}}
            )
        return httpx.Response(404, json={"ok": False})

    a = mod.TelegramAdapter({"bot_token": "T"})
    a._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0)
    a._bot_id = 7
    a._sent = sent  # type: ignore[attr-defined]
    return a


@pytest.mark.asyncio
async def test_telegram_steer_prefix_routes_to_registry(
    telegram_adapter, monkeypatch
) -> None:
    """``/steer <text>`` must NOT reach the gateway dispatcher."""
    from opencomputer.agent import steer as steer_mod
    from opencomputer.agent.steer import SteerRegistry

    fresh = SteerRegistry()
    monkeypatch.setattr(steer_mod, "default_registry", fresh, raising=True)

    forwarded: list = []

    async def handler(event):
        forwarded.append(event)
        return None

    telegram_adapter.set_message_handler(handler)

    update = {
        "update_id": 1,
        "message": {
            "message_id": 100,
            "date": 1700000000,
            "from": {"id": 999},
            "chat": {"id": 555},
            "text": "/steer focus on the failing test",
        },
    }
    await telegram_adapter._handle_update(update)

    # Critical invariant: the message handler (gateway dispatcher) was
    # NEVER called for this message.
    assert forwarded == []

    # The nudge landed in the registry under the dispatcher's session id.
    from opencomputer.gateway.dispatch import session_id_for
    from plugin_sdk.core import Platform

    sid = session_id_for(Platform.TELEGRAM.value, "555")
    assert fresh.consume(sid) == "focus on the failing test"

    # The user got an ack via sendMessage.
    assert telegram_adapter._sent  # type: ignore[attr-defined]
    ack = telegram_adapter._sent[-1]  # type: ignore[attr-defined]
    assert "steer queued" in ack["text"].lower()


@pytest.mark.asyncio
async def test_telegram_steer_empty_body_sends_usage_hint(
    telegram_adapter, monkeypatch
) -> None:
    from opencomputer.agent import steer as steer_mod
    from opencomputer.agent.steer import SteerRegistry

    fresh = SteerRegistry()
    monkeypatch.setattr(steer_mod, "default_registry", fresh, raising=True)

    async def handler(event):
        return None

    telegram_adapter.set_message_handler(handler)

    update = {
        "update_id": 2,
        "message": {
            "message_id": 101,
            "date": 1700000001,
            "from": {"id": 999},
            "chat": {"id": 555},
            "text": "/steer ",  # nothing after the prefix
        },
    }
    await telegram_adapter._handle_update(update)

    # Registry untouched — empty body means we send a usage hint, not a
    # silent no-op nudge.
    from opencomputer.gateway.dispatch import session_id_for
    from plugin_sdk.core import Platform

    sid = session_id_for(Platform.TELEGRAM.value, "555")
    assert fresh.consume(sid) is None
    assert telegram_adapter._sent  # type: ignore[attr-defined]
    assert "usage" in telegram_adapter._sent[-1]["text"].lower()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_telegram_steer_override_surfaces_in_ack(
    telegram_adapter, monkeypatch
) -> None:
    """A second /steer in the same chat acks ``previous nudge discarded``."""
    from opencomputer.agent import steer as steer_mod
    from opencomputer.agent.steer import SteerRegistry

    fresh = SteerRegistry()
    monkeypatch.setattr(steer_mod, "default_registry", fresh, raising=True)

    async def handler(event):
        return None

    telegram_adapter.set_message_handler(handler)

    base_update = {
        "update_id": 3,
        "message": {
            "message_id": 200,
            "date": 1700000002,
            "from": {"id": 999},
            "chat": {"id": 555},
            "text": "/steer first",
        },
    }
    await telegram_adapter._handle_update(base_update)

    base_update["update_id"] = 4
    base_update["message"] = dict(base_update["message"])
    base_update["message"]["text"] = "/steer second"
    await telegram_adapter._handle_update(base_update)

    # Two acks; the second mentions the override.
    sent = telegram_adapter._sent  # type: ignore[attr-defined]
    assert len(sent) == 2
    assert "override" not in sent[0]["text"].lower()
    assert "override" in sent[1]["text"].lower()


@pytest.mark.asyncio
async def test_telegram_non_steer_message_still_dispatched(
    telegram_adapter, monkeypatch
) -> None:
    """Plain messages must still reach the gateway dispatcher."""
    from opencomputer.agent import steer as steer_mod
    from opencomputer.agent.steer import SteerRegistry

    fresh = SteerRegistry()
    monkeypatch.setattr(steer_mod, "default_registry", fresh, raising=True)

    forwarded: list = []

    async def handler(event):
        forwarded.append(event)
        return None

    telegram_adapter.set_message_handler(handler)

    update = {
        "update_id": 9,
        "message": {
            "message_id": 300,
            "date": 1700000005,
            "from": {"id": 999},
            "chat": {"id": 555},
            "text": "hello agent",
        },
    }
    await telegram_adapter._handle_update(update)

    assert len(forwarded) == 1
    assert forwarded[0].text == "hello agent"

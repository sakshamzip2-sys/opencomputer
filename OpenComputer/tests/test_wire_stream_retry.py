"""Wire-server surface for pre-first-byte streaming retry.

Two layers covered:

* **Protocol surface** — :data:`EVENT_STREAM_RETRY` constant exists and
  is exported, :class:`StreamRetryPayload` validates required fields,
  :data:`EVENT_SCHEMAS` registry maps the event to the schema, and the
  ``hello`` handshake advertises the new event so capability-detecting
  clients see it.

* **End-to-end wire bridge** — ``WireServer._handle_chat`` passes a
  ``retry_callback`` to ``loop.run_conversation``. When the agent loop
  invokes that callback during a recovery window, the WS client
  receives a ``stream.retry`` event with the matching request_id and
  the RetryStatus fields passed through.

Mirrors the test layout of ``tests/test_wire_memory_broadcast.py`` and
``tests/test_wire_evolution_bridge.py``.
"""

from __future__ import annotations

import asyncio
import json
import socket
from unittest.mock import AsyncMock, MagicMock

import pytest
import websockets


async def _find_free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _build_loop_that_fires_retry(retry_statuses):
    """Build a fake AgentLoop whose run_conversation invokes the
    supplied retry_callback with each entry in ``retry_statuses``
    before returning a normal ConversationResult.

    Mirrors the test pattern from ``tests/test_phase9.py:_build_fake_loop``.
    """
    from opencomputer.agent.loop import ConversationResult
    from plugin_sdk.core import Message

    loop = MagicMock()
    final = Message(role="assistant", content="recovered")
    result = ConversationResult(
        final_message=final,
        messages=[final],
        session_id="retry-session",
        iterations=1,
        input_tokens=12,
        output_tokens=2,
    )

    async def _fake_run_conversation(*, retry_callback=None, **_kwargs):
        # Simulate the AgentLoop's stream_retry wrapper firing the
        # callback during a recovery window before the final success.
        if retry_callback is not None:
            for status in retry_statuses:
                retry_callback(status)
                # Yield control so the WS broadcast task scheduled by
                # the callback gets a chance to run before we return.
                await asyncio.sleep(0)
        return result

    loop.run_conversation = AsyncMock(side_effect=_fake_run_conversation)
    return loop


# ─── Protocol surface ──────────────────────────────────────────────────


class TestStreamRetryProtocolSurface:
    def test_event_constant(self) -> None:
        from opencomputer.gateway.protocol import EVENT_STREAM_RETRY

        assert EVENT_STREAM_RETRY == "stream.retry"

    def test_event_constant_exported(self) -> None:
        from opencomputer.gateway import protocol

        assert "EVENT_STREAM_RETRY" in protocol.__all__

    def test_payload_required_fields(self) -> None:
        from opencomputer.gateway.protocol_v2 import StreamRetryPayload

        p = StreamRetryPayload(
            request_id="req-1",
            attempt=1,
            next_attempt=2,
            max_attempts=4,
            delay_seconds=1.3,
            error_kind="overloaded",
            error_message="HTTP 529 overloaded_error",
            exhausted=False,
        )
        assert p.request_id == "req-1"
        assert p.attempt == 1
        assert p.next_attempt == 2
        assert p.max_attempts == 4
        assert p.delay_seconds == 1.3
        assert p.error_kind == "overloaded"
        assert p.exhausted is False

    def test_payload_exhausted_form(self) -> None:
        """Exhausted form: attempt == next_attempt, delay_seconds=0."""
        from opencomputer.gateway.protocol_v2 import StreamRetryPayload

        p = StreamRetryPayload(
            request_id="req-final",
            attempt=4,
            next_attempt=4,
            max_attempts=4,
            delay_seconds=0.0,
            error_kind="overloaded",
            error_message="HTTP 529 overloaded_error: still down",
            exhausted=True,
        )
        assert p.exhausted is True
        assert p.delay_seconds == 0.0

    def test_payload_round_trip_json(self) -> None:
        from opencomputer.gateway.protocol_v2 import StreamRetryPayload

        original = StreamRetryPayload(
            request_id="req-rt",
            attempt=2,
            next_attempt=3,
            max_attempts=4,
            delay_seconds=2.1,
            error_kind="bad_gateway",
            error_message="HTTP 502 bad gateway",
            exhausted=False,
        )
        s = original.model_dump_json()
        restored = StreamRetryPayload.model_validate_json(s)
        assert restored == original

    def test_payload_rejects_unknown_field(self) -> None:
        """Strict model — wire callers can't graft on stale fields."""
        from opencomputer.gateway.protocol_v2 import StreamRetryPayload

        with pytest.raises(Exception):  # pydantic.ValidationError
            StreamRetryPayload(
                request_id="req-1",
                attempt=1,
                next_attempt=2,
                max_attempts=4,
                delay_seconds=0.5,
                error_kind="overloaded",
                error_message="x",
                exhausted=False,
                bogus_field="surprise",
            )

    def test_event_schemas_registry(self) -> None:
        from opencomputer.gateway.protocol import EVENT_STREAM_RETRY
        from opencomputer.gateway.protocol_v2 import (
            EVENT_SCHEMAS,
            StreamRetryPayload,
        )

        assert EVENT_SCHEMAS[EVENT_STREAM_RETRY] is StreamRetryPayload

    def test_payload_exported_from_protocol_v2(self) -> None:
        from opencomputer.gateway import protocol_v2

        assert "StreamRetryPayload" in protocol_v2.__all__
        assert "EVENT_STREAM_RETRY" in protocol_v2.__all__


# ─── Hello handshake advertisement ─────────────────────────────────────


@pytest.mark.asyncio
async def test_hello_handshake_advertises_stream_retry() -> None:
    """A connecting client must see ``stream.retry`` in HelloResult.events
    so capability-detecting TUIs / IDE clients know to subscribe / render
    the banner panel.
    """
    from opencomputer.gateway.wire_server import WireServer

    fake_loop = _build_loop_that_fires_retry([])
    port = await _find_free_port()
    server = WireServer(loop=fake_loop, port=port)
    await server.start()
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.send(
                json.dumps(
                    {
                        "type": "req",
                        "id": "h1",
                        "method": "hello",
                        "params": {"client": "test", "version": "0.0.1"},
                    }
                )
            )
            # Read response (might be preceded by replay events, but a
            # fresh connection has none).
            for _ in range(5):
                raw = await asyncio.wait_for(ws.recv(), timeout=3.0)
                msg = json.loads(raw)
                if msg.get("type") == "res" and msg.get("id") == "h1":
                    events = msg["payload"]["events"]
                    assert "stream.retry" in events, events
                    break
            else:
                pytest.fail("hello response never arrived")
    finally:
        await server.stop()


# ─── End-to-end: chat call surfaces retry status to WS client ──────────


@pytest.mark.asyncio
async def test_chat_retry_status_broadcasts_to_ws_client() -> None:
    """When AgentLoop invokes the retry_callback supplied by WireServer,
    the connected WS client receives a ``stream.retry`` event payload
    that round-trips the RetryStatus fields and is tagged with the
    triggering request_id.
    """
    from opencomputer.agent.stream_retry import RetryStatus
    from opencomputer.gateway.wire_server import WireServer

    statuses = [
        RetryStatus(
            attempt=1,
            next_attempt=2,
            max_attempts=4,
            delay_seconds=1.3,
            error_kind="overloaded",
            error_message="HTTP 529 overloaded_error: Overloaded",
            exhausted=False,
        ),
    ]
    fake_loop = _build_loop_that_fires_retry(statuses)
    port = await _find_free_port()
    server = WireServer(loop=fake_loop, port=port)
    await server.start()
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.send(
                json.dumps(
                    {
                        "type": "req",
                        "id": "chat-1",
                        "method": "chat",
                        "params": {"message": "hi"},
                    }
                )
            )
            seen_retry_payloads: list[dict] = []
            response = None
            for _ in range(20):
                raw = await asyncio.wait_for(ws.recv(), timeout=3.0)
                msg = json.loads(raw)
                if msg.get("type") == "event" and msg.get("event") == "stream.retry":
                    seen_retry_payloads.append(msg["payload"])
                elif msg.get("type") == "res" and msg.get("id") == "chat-1":
                    response = msg
                    break
            assert response is not None
            assert response["ok"] is True
            assert len(seen_retry_payloads) == 1, seen_retry_payloads
            pl = seen_retry_payloads[0]
            assert pl["request_id"] == "chat-1"
            assert pl["attempt"] == 1
            assert pl["next_attempt"] == 2
            assert pl["max_attempts"] == 4
            assert pl["delay_seconds"] == 1.3
            assert pl["error_kind"] == "overloaded"
            assert pl["exhausted"] is False
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_chat_retry_exhaustion_status_broadcasts() -> None:
    """The ``exhausted=True`` final status is also broadcast — clients
    rendering the banner need both transitions to switch from
    "retrying" to "exhausted" before the response surfaces.
    """
    from opencomputer.agent.stream_retry import RetryStatus
    from opencomputer.gateway.wire_server import WireServer

    statuses = [
        RetryStatus(
            attempt=1,
            next_attempt=2,
            max_attempts=2,
            delay_seconds=0.5,
            error_kind="overloaded",
            error_message="HTTP 529 spike-1",
            exhausted=False,
        ),
        RetryStatus(
            attempt=2,
            next_attempt=2,
            max_attempts=2,
            delay_seconds=0.0,
            error_kind="overloaded",
            error_message="HTTP 529 spike-2",
            exhausted=True,
        ),
    ]
    fake_loop = _build_loop_that_fires_retry(statuses)
    port = await _find_free_port()
    server = WireServer(loop=fake_loop, port=port)
    await server.start()
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.send(
                json.dumps(
                    {
                        "type": "req",
                        "id": "chat-x",
                        "method": "chat",
                        "params": {"message": "hi"},
                    }
                )
            )
            seen: list[dict] = []
            for _ in range(20):
                raw = await asyncio.wait_for(ws.recv(), timeout=3.0)
                msg = json.loads(raw)
                if msg.get("type") == "event" and msg.get("event") == "stream.retry":
                    seen.append(msg["payload"])
                elif msg.get("type") == "res" and msg.get("id") == "chat-x":
                    break
            assert len(seen) == 2
            assert seen[0]["exhausted"] is False
            assert seen[1]["exhausted"] is True
            assert seen[0]["request_id"] == "chat-x"
            assert seen[1]["request_id"] == "chat-x"
    finally:
        await server.stop()

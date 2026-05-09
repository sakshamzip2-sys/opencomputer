"""M3.1 + M3.3 — wire-protocol completeness.

Pins the contract added 2026-05-09:

* M3.1 — ``EVENT_PERMISSION_REQUEST`` event + ``METHOD_PERMISSION_RESPONSE``
  RPC + the typed ``PermissionRequestPayload`` /
  ``PermissionResponseParams`` / ``PermissionResponseResult`` schemas.
* M3.3 — per-session ring buffer of last 200 events with monotonic
  ``WireEvent.seq`` field + replay-on-hello via
  ``HelloParams.last_event_seq`` returning ``HelloResult.gap_warning``.

These tests exercise the WireServer's internal helpers directly
(``_send_event``, ``_replay_after_hello``, ``broadcast_permission_request``)
without standing up a real WebSocket — the JSON-RPC dispatch is
covered by the existing wire integration tests.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from opencomputer.gateway.protocol import (
    EVENT_PERMISSION_REQUEST,
    METHOD_PERMISSION_RESPONSE,
    WireEvent,
)
from opencomputer.gateway.protocol_v2 import (
    EVENT_SCHEMAS,
    METHOD_SCHEMAS,
    HelloParams,
    HelloResult,
    PermissionRequestPayload,
    PermissionResponseParams,
    PermissionResponseResult,
)
from opencomputer.gateway.wire_server import RING_BUFFER_MAX, WireServer

# ─── Protocol surface (M3.1) ─────────────────────────────────────────────


class TestProtocolSurface:
    def test_method_permission_response_constant(self) -> None:
        assert METHOD_PERMISSION_RESPONSE == "permission.response"

    def test_event_permission_request_constant(self) -> None:
        assert EVENT_PERMISSION_REQUEST == "permission.request"

    def test_method_schemas_includes_permission_response(self) -> None:
        params_cls, result_cls = METHOD_SCHEMAS[METHOD_PERMISSION_RESPONSE]
        assert params_cls is PermissionResponseParams
        assert result_cls is PermissionResponseResult

    def test_event_schemas_includes_permission_request(self) -> None:
        assert EVENT_SCHEMAS[EVENT_PERMISSION_REQUEST] is PermissionRequestPayload

    def test_permission_response_decision_validated(self) -> None:
        # Strict pydantic — Literal["allow_once","allow_always","deny"]
        ok = PermissionResponseParams(
            request_id="r1",
            session_id="s1",
            capability_id="bash.execute",
            decision="allow_once",
        )
        assert ok.decision == "allow_once"
        with pytest.raises(Exception):  # pydantic.ValidationError
            PermissionResponseParams(
                request_id="r1",
                session_id="s1",
                capability_id="bash.execute",
                decision="maybe",
            )

    def test_permission_request_payload_optional_scope(self) -> None:
        # scope/context default to None / empty so a Tier-2 ask without
        # a resource still constructs cleanly.
        p = PermissionRequestPayload(
            request_id="r",
            session_id="s",
            capability_id="net.fetch",
        )
        assert p.scope is None
        assert p.context == ""
        assert p.timeout_s == 300.0


# ─── Ring-buffer + seq (M3.3) ────────────────────────────────────────────


class TestWireEventSeq:
    def test_seq_field_default_none(self) -> None:
        ev = WireEvent(event="turn.begin", payload={})
        assert ev.seq is None  # backwards compat — old clients stay unaffected

    def test_seq_field_round_trips(self) -> None:
        ev = WireEvent(event="turn.begin", payload={"x": 1}, seq=42)
        d = json.loads(ev.model_dump_json())
        assert d["seq"] == 42


@pytest.fixture
def server() -> WireServer:
    """Build a WireServer with a stub AgentLoop. We only exercise the
    ring-buffer + permission helpers, never start the websocket."""
    loop = MagicMock()
    loop.db.list_sessions.return_value = []
    return WireServer(loop=loop)


def _fake_ws() -> AsyncMock:
    ws = AsyncMock()
    ws.send = AsyncMock()
    return ws


class TestRingBufferAccumulation:
    @pytest.mark.asyncio
    async def test_send_event_stamps_monotonic_seq(self, server: WireServer) -> None:
        ws = _fake_ws()
        sid = "sess-A"
        await server._send_event(ws, "turn.begin", {"session_id": sid})
        await server._send_event(ws, "tool.call", {"session_id": sid})
        await server._send_event(ws, "turn.end", {"session_id": sid})

        assert server._session_seq[sid] == 3
        assert len(server._session_rings[sid]) == 3
        seqs = [e.seq for e in server._session_rings[sid]]
        assert seqs == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_send_event_writes_to_ws(self, server: WireServer) -> None:
        ws = _fake_ws()
        await server._send_event(ws, "turn.begin", {"session_id": "x"})
        ws.send.assert_awaited_once()
        sent = json.loads(ws.send.await_args.args[0])
        assert sent["event"] == "turn.begin"
        assert sent["seq"] == 1

    @pytest.mark.asyncio
    async def test_no_session_id_means_no_seq_no_buffer(
        self, server: WireServer
    ) -> None:
        ws = _fake_ws()
        # Anonymous event (no session_id, no request_id) → not buffered.
        await server._send_event(ws, "error", {"oops": "no-context"})
        assert "" not in server._session_rings
        sent = json.loads(ws.send.await_args.args[0])
        assert sent["seq"] is None  # not stamped

    @pytest.mark.asyncio
    async def test_ring_buffer_evicts_at_capacity(
        self, server: WireServer
    ) -> None:
        ws = _fake_ws()
        sid = "long-running"
        # Send 250 events — buffer caps at RING_BUFFER_MAX=200.
        for i in range(250):
            await server._send_event(
                ws, "tool.call", {"session_id": sid, "i": i}
            )
        assert len(server._session_rings[sid]) == RING_BUFFER_MAX
        assert server._session_seq[sid] == 250
        # Oldest in the buffer should be seq 51 (first 50 evicted).
        oldest = server._session_rings[sid][0]
        newest = server._session_rings[sid][-1]
        assert oldest.seq == 51
        assert newest.seq == 250


class TestReplayAfterHello:
    @pytest.mark.asyncio
    async def test_replay_returns_events_after_last_seq(
        self, server: WireServer
    ) -> None:
        ws = _fake_ws()
        sid = "s"
        for _ in range(5):
            await server._send_event(ws, "tool.call", {"session_id": sid})

        last_seq, gap, replay = server._replay_after_hello(ws, sid, 2)
        assert last_seq == 5
        assert gap is False
        assert [e.seq for e in replay] == [3, 4, 5]

    @pytest.mark.asyncio
    async def test_replay_empty_when_caught_up(
        self, server: WireServer
    ) -> None:
        ws = _fake_ws()
        sid = "s"
        for _ in range(3):
            await server._send_event(ws, "tool.call", {"session_id": sid})
        last_seq, gap, replay = server._replay_after_hello(ws, sid, 3)
        assert last_seq == 3
        assert gap is False
        assert replay == []

    @pytest.mark.asyncio
    async def test_replay_gap_warning_when_overflow(
        self, server: WireServer
    ) -> None:
        ws = _fake_ws()
        sid = "long-running"
        # Send more events than the buffer holds; ask to replay from 1.
        for _ in range(RING_BUFFER_MAX + 50):
            await server._send_event(ws, "tool.call", {"session_id": sid})
        last_seq, gap, replay = server._replay_after_hello(ws, sid, 1)
        assert gap is True
        assert last_seq == RING_BUFFER_MAX + 50
        # Replay covers everything currently in the ring.
        assert len(replay) == RING_BUFFER_MAX

    @pytest.mark.asyncio
    async def test_replay_unknown_session_returns_empty(
        self, server: WireServer
    ) -> None:
        ws = _fake_ws()
        last_seq, gap, replay = server._replay_after_hello(ws, "never-seen", 0)
        assert last_seq == 0
        assert gap is False
        assert replay == []


# ─── Permission request producer (M3.1) ──────────────────────────────────


class TestBroadcastPermissionRequest:
    @pytest.mark.asyncio
    async def test_broadcast_to_registered_clients(
        self, server: WireServer
    ) -> None:
        ws_a = _fake_ws()
        ws_b = _fake_ws()
        sid = "s"
        server._session_clients[sid] = {ws_a, ws_b}

        delivered = await server.broadcast_permission_request(
            session_id=sid,
            request_id="req-1",
            capability_id="bash.execute",
            scope="/tmp/x",
            context="agent wants to clean tmp",
        )
        assert delivered == 2
        ws_a.send.assert_awaited_once()
        ws_b.send.assert_awaited_once()

        sent = json.loads(ws_a.send.await_args.args[0])
        assert sent["event"] == EVENT_PERMISSION_REQUEST
        assert sent["payload"]["capability_id"] == "bash.execute"
        assert sent["payload"]["scope"] == "/tmp/x"

    @pytest.mark.asyncio
    async def test_broadcast_zero_when_no_clients(
        self, server: WireServer
    ) -> None:
        delivered = await server.broadcast_permission_request(
            session_id="lonely-session",
            request_id="r",
            capability_id="x",
        )
        assert delivered == 0

    @pytest.mark.asyncio
    async def test_broadcast_swallows_one_client_failure(
        self, server: WireServer
    ) -> None:
        ws_ok = _fake_ws()
        ws_dead = _fake_ws()
        ws_dead.send.side_effect = ConnectionError("client died")
        sid = "mixed-state"
        server._session_clients[sid] = {ws_ok, ws_dead}

        # Don't crash on the dead client; the OK one still got it.
        delivered = await server.broadcast_permission_request(
            session_id=sid, request_id="r", capability_id="x"
        )
        assert delivered == 1
        ws_ok.send.assert_awaited_once()


# ─── HelloParams / HelloResult (M3.3) ────────────────────────────────────


class TestHelloParamsHelloResult:
    def test_hello_params_session_id_and_last_event_seq_optional(self) -> None:
        # Old clients omit both; the model still constructs cleanly.
        h = HelloParams(client="opencomputer-tui/0.1.0")
        assert h.session_id is None
        assert h.last_event_seq is None

    def test_hello_params_with_replay_request(self) -> None:
        h = HelloParams(
            client="opencomputer-ide/2.0",
            session_id="s-resume",
            last_event_seq=42,
        )
        assert h.session_id == "s-resume"
        assert h.last_event_seq == 42

    def test_hello_result_carries_gap_warning(self) -> None:
        r = HelloResult(
            server="opencomputer/0.1.0",
            capabilities=("chat",),
            gap_warning=True,
            server_last_event_seq=350,
        )
        assert r.gap_warning is True
        assert r.server_last_event_seq == 350

    def test_hello_result_defaults_no_gap(self) -> None:
        r = HelloResult(server="oc", capabilities=())
        assert r.gap_warning is False
        assert r.server_last_event_seq is None

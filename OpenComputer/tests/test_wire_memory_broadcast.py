"""Tier-C wire-bridge tests — closes spec line 232 (TUI memory panel gap).

Covers:

* **M1 protocol surface** — ``EVENT_MEMORY_WRITE`` constant + ``MemoryWritePayload``
  pydantic schema + ``EVENT_SCHEMAS`` registry entry. Mirrors the pattern from
  ``EVENT_PERMISSION_REQUEST`` (M3.1, 2026-05-09).
* **M2 bus→wire bridge** — ``WireServer`` subscribes to ``default_bus`` for
  ``MemoryWriteEvent`` on ``start()``; broadcasts a ``WireEvent(event="memory.write")``
  to every connected WS client via the new ``_session_clients_all`` set;
  unsubscribes on ``stop()``.

Closes the silent-compaction last-mile: PR #588 (M2 of 2026-05-10 memory-observability
design) added ``compaction_delta`` and ``dropped_paragraphs`` to ``MemoryWriteEvent``,
but the wire surface never carried the event. A TUI client at ``ws://127.0.0.1:18789``
now sees compaction events in real time and can render a memory-status panel.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from typing import Any

import pytest

# ─── M1: protocol surface ──────────────────────────────────────────────


class TestMemoryWriteProtocolSurface:
    """Pin the wire-protocol surface added for memory.write events."""

    def test_event_memory_write_constant(self) -> None:
        from opencomputer.gateway.protocol import EVENT_MEMORY_WRITE

        assert EVENT_MEMORY_WRITE == "memory.write"

    def test_event_memory_write_in_protocol_all(self) -> None:
        from opencomputer.gateway import protocol

        assert "EVENT_MEMORY_WRITE" in protocol.__all__

    def test_memory_write_payload_required_fields(self) -> None:
        from opencomputer.gateway.protocol_v2 import MemoryWritePayload

        p = MemoryWritePayload(
            action="append",
            target="MEMORY.md",
            content_size=2821,
            cap_limit=4000,
            compaction_delta=0,
            dropped_paragraphs=0,
        )
        assert p.action == "append"
        assert p.target == "MEMORY.md"
        assert p.content_size == 2821
        assert p.cap_limit == 4000

    def test_memory_write_payload_compaction_defaults(self) -> None:
        # Non-compacting writes don't have to pass the delta fields;
        # they should default to 0 and round-trip cleanly.
        from opencomputer.gateway.protocol_v2 import MemoryWritePayload

        p = MemoryWritePayload(
            action="replace",
            target="USER.md",
            content_size=1785,
            cap_limit=2000,
        )
        assert p.compaction_delta == 0
        assert p.dropped_paragraphs == 0

    def test_memory_write_payload_round_trip_json(self) -> None:
        from opencomputer.gateway.protocol_v2 import MemoryWritePayload

        original = MemoryWritePayload(
            action="append",
            target="MEMORY.md",
            content_size=3480,
            cap_limit=4000,
            compaction_delta=520,
            dropped_paragraphs=2,
        )
        s = original.model_dump_json()
        restored = MemoryWritePayload.model_validate_json(s)
        assert restored == original

    def test_memory_write_payload_rejects_unknown_field(self) -> None:
        # _StrictModel uses extra="forbid" — wire callers can't accidentally
        # graft on stale fields without a deliberate schema bump.
        from opencomputer.gateway.protocol_v2 import MemoryWritePayload

        with pytest.raises(Exception):  # pydantic.ValidationError
            MemoryWritePayload(
                action="append",
                target="MEMORY.md",
                content_size=100,
                cap_limit=4000,
                bogus_field="surprise",
            )

    def test_event_schemas_registry_includes_memory_write(self) -> None:
        from opencomputer.gateway.protocol import EVENT_MEMORY_WRITE
        from opencomputer.gateway.protocol_v2 import EVENT_SCHEMAS, MemoryWritePayload

        assert EVENT_SCHEMAS[EVENT_MEMORY_WRITE] is MemoryWritePayload


# ─── M2: bus→wire bridge ──────────────────────────────────────────────
#
# These tests stand up a real ``WireServer`` against a free ephemeral
# port and connect ``websockets.connect`` clients to verify the broadcast
# path end-to-end. The publisher path in ``MemoryManager`` already has
# unit coverage at ``tests/test_memory_event_compaction.py``; here we
# only verify the wire bridge.


@contextlib.asynccontextmanager
async def _wire_server_on_free_port():
    """Spin up a real WireServer on a free port; yield (server, ws_url)."""
    import socket

    from opencomputer.agent.loop import AgentLoop
    from opencomputer.gateway.wire_server import WireServer

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    # Construct a minimal AgentLoop without touching providers — the
    # broadcast tests don't dispatch chat. We use a stub via the legacy
    # loop= path with a MagicMock that satisfies the router-wrapping
    # __init__ (which only stores it).
    from unittest.mock import MagicMock

    fake_loop = MagicMock(spec=AgentLoop)
    server = WireServer(loop=fake_loop, host="127.0.0.1", port=port)
    await server.start()
    try:
        yield server, f"ws://127.0.0.1:{port}"
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_bridge_broadcasts_memory_write_event_to_all_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Publishing a MemoryWriteEvent on default_bus must reach every connected WS."""
    import websockets

    from opencomputer.ingestion.bus import default_bus
    from plugin_sdk.ingestion import MemoryWriteEvent

    async with _wire_server_on_free_port() as (_server, url):
        client_a = await websockets.connect(url)
        client_b = await websockets.connect(url)
        try:
            # Settle: both clients are now in _session_clients_all.
            await asyncio.sleep(0.05)

            # Publish a memory write event with non-zero compaction.
            ev = MemoryWriteEvent(
                session_id=None,
                source="agent_memory",
                action="append",
                target="MEMORY.md",
                content_size=3480,
                compaction_delta=520,
                dropped_paragraphs=2,
            )
            default_bus.publish(ev)

            # Give the bridge a tick to schedule the broadcast.
            msg_a = await asyncio.wait_for(client_a.recv(), timeout=2.0)
            msg_b = await asyncio.wait_for(client_b.recv(), timeout=2.0)

            for msg in (msg_a, msg_b):
                data = json.loads(msg)
                assert data["type"] == "event"
                assert data["event"] == "memory.write"
                pl = data["payload"]
                assert pl["action"] == "append"
                assert pl["target"] == "MEMORY.md"
                assert pl["content_size"] == 3480
                assert pl["compaction_delta"] == 520
                assert pl["dropped_paragraphs"] == 2
                assert pl["cap_limit"] == 4000
        finally:
            await client_a.close()
            await client_b.close()


@pytest.mark.asyncio
async def test_bridge_user_md_carries_2000_cap_limit() -> None:
    """USER.md events report cap_limit=2000 (not 4000)."""
    import websockets

    from opencomputer.ingestion.bus import default_bus
    from plugin_sdk.ingestion import MemoryWriteEvent

    async with _wire_server_on_free_port() as (_server, url):
        client = await websockets.connect(url)
        try:
            await asyncio.sleep(0.05)
            default_bus.publish(MemoryWriteEvent(
                session_id=None,
                source="agent_memory",
                action="append",
                target="USER.md",
                content_size=1785,
            ))
            data = json.loads(await asyncio.wait_for(client.recv(), timeout=2.0))
            assert data["payload"]["cap_limit"] == 2000
            assert data["payload"]["target"] == "USER.md"
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_bridge_unsubscribes_on_stop() -> None:
    """After server.stop(), the bus subscription must be released."""
    from opencomputer.ingestion.bus import default_bus
    from plugin_sdk.ingestion import MemoryWriteEvent

    pre_count = len(default_bus.subscribers("memory_write"))

    async with _wire_server_on_free_port() as (server, _url):
        # Bridge subscribed at start().
        assert len(default_bus.subscribers("memory_write")) == pre_count + 1

    # After stop() (exit of context), back to baseline.
    assert len(default_bus.subscribers("memory_write")) == pre_count

    # Sanity: publishing now is a noop with no observable side effect.
    default_bus.publish(MemoryWriteEvent(
        session_id=None,
        source="agent_memory",
        action="append",
        target="MEMORY.md",
        content_size=10,
    ))


@pytest.mark.asyncio
async def test_bridge_broadcast_with_no_clients_does_not_raise() -> None:
    """Empty _session_clients_all set must not raise on broadcast."""
    from opencomputer.ingestion.bus import default_bus
    from plugin_sdk.ingestion import MemoryWriteEvent

    async with _wire_server_on_free_port() as (_server, _url):
        # No clients connected. Publishing should be safe.
        default_bus.publish(MemoryWriteEvent(
            session_id=None,
            source="agent_memory",
            action="remove",
            target="MEMORY.md",
            content_size=42,
        ))
        # Settle to let any (no-op) broadcast schedule + complete.
        await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_bridge_debug_env_var_logs_at_debug_level(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """OPENCOMPUTER_WIRE_DEBUG_EVENTS=1 emits a DEBUG line per broadcast."""
    import logging

    from opencomputer.ingestion.bus import default_bus
    from plugin_sdk.ingestion import MemoryWriteEvent

    monkeypatch.setenv("OPENCOMPUTER_WIRE_DEBUG_EVENTS", "1")
    caplog.set_level(logging.DEBUG, logger="opencomputer.gateway.wire_server")

    async with _wire_server_on_free_port() as (_server, _url):
        default_bus.publish(MemoryWriteEvent(
            session_id=None,
            source="agent_memory",
            action="append",
            target="MEMORY.md",
            content_size=42,
        ))
        await asyncio.sleep(0.05)

    msgs = [r.getMessage() for r in caplog.records if "memory.write" in r.getMessage()]
    assert any("memory.write" in m for m in msgs), msgs

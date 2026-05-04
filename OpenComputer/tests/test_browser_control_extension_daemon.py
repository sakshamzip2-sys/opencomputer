"""End-to-end daemon ↔ stub-extension tests via real local WebSocket.

Spawns the production ``ControlDaemon`` on an ephemeral port, connects
a stub WebSocket client (impersonating the extension), and verifies:
  - hello → contextId registration
  - Command JSON → stub → Result JSON correlation by id
  - Concurrent commands demux correctly
  - Action gate rejects unsupported v0.6 actions cleanly
  - Disconnect cancels pending futures with a clear error

These tests need a running event loop + a free TCP port; they're real
integration tests, not unit tests with mocks.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket

import pytest
from extensions.browser_control.control_daemon import (
    ActionNotSupportedError,
    CommandTimeoutError,
    ControlDaemon,
    ControlDaemonError,
)
from extensions.browser_control.control_protocol import Command


def _free_port() -> int:
    """Find an unused TCP port for an isolated daemon under test."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _start_daemon(port: int) -> ControlDaemon:
    daemon = ControlDaemon(port=port, command_timeout_s=2.0)
    await daemon.start()
    # Give the WS server a tick to bind.
    await asyncio.sleep(0.05)
    return daemon


@contextlib.asynccontextmanager
async def _stub_extension_ws(port: int, *, context_id: str = "test"):
    """Connect a stub extension client (real WebSocket) to the daemon.

    Yields a (recv_one, send_one) pair plus a `tasks` list the test can
    await for orderly shutdown.
    """
    import websockets  # type: ignore[import-not-found]

    ws = await websockets.connect(f"ws://127.0.0.1:{port}/ext")
    # Send the hello first so daemon registers the extension.
    await ws.send(
        json.dumps(
            {
                "type": "hello",
                "contextId": context_id,
                "version": "0.6.0",
                "compatRange": "^0.6.0",
            }
        )
    )
    try:
        yield ws
    finally:
        await ws.close()


@pytest.mark.asyncio
async def test_daemon_starts_and_stops_cleanly() -> None:
    daemon = await _start_daemon(_free_port())
    try:
        # No extensions connected yet.
        assert daemon.extensions == {}
    finally:
        await daemon.stop()


@pytest.mark.asyncio
async def test_hello_registers_extension() -> None:
    port = _free_port()
    daemon = await _start_daemon(port)
    try:
        async with _stub_extension_ws(port, context_id="user") as _ws:
            # Give daemon a tick to process the hello.
            await asyncio.sleep(0.1)
            assert "user" in daemon.extensions
            ext = daemon.extensions["user"]
            assert ext.extension_version == "0.6.0"
            assert ext.compat_range == "^0.6.0"
    finally:
        await daemon.stop()


@pytest.mark.asyncio
async def test_command_round_trip() -> None:
    """Daemon sends Command, stub returns Result, daemon resolves the future."""
    port = _free_port()
    daemon = await _start_daemon(port)
    try:
        async with _stub_extension_ws(port, context_id="user") as ws:
            await asyncio.sleep(0.1)  # allow hello to register

            async def stub_responder() -> None:
                # Read one Command from the daemon, echo back a Result.
                raw = await ws.recv()
                cmd = json.loads(raw)
                assert cmd["action"] == "navigate"
                assert cmd["url"] == "https://example.com"
                await ws.send(
                    json.dumps(
                        {
                            "id": cmd["id"],
                            "ok": True,
                            "data": {"navigated": True},
                        }
                    )
                )

            stub_task = asyncio.create_task(stub_responder())

            cmd = daemon.make_command("navigate", url="https://example.com")
            result = await daemon.send(cmd, context_id="user")

            assert result.ok is True
            assert result.data == {"navigated": True}
            await stub_task
    finally:
        await daemon.stop()


@pytest.mark.asyncio
async def test_concurrent_commands_demux_by_id() -> None:
    """Two in-flight commands must resolve to their respective Results, not swap."""
    port = _free_port()
    daemon = await _start_daemon(port)
    try:
        async with _stub_extension_ws(port, context_id="user") as ws:
            await asyncio.sleep(0.1)

            async def stub_responder() -> None:
                # Read both Commands, then respond out-of-order.
                cmd1_raw = await ws.recv()
                cmd2_raw = await ws.recv()
                cmd1 = json.loads(cmd1_raw)
                cmd2 = json.loads(cmd2_raw)
                # Respond to cmd2 first, then cmd1 — verifies demux.
                await ws.send(json.dumps({"id": cmd2["id"], "ok": True, "data": "second"}))
                await ws.send(json.dumps({"id": cmd1["id"], "ok": True, "data": "first"}))

            stub_task = asyncio.create_task(stub_responder())

            cmd1 = daemon.make_command("navigate", url="https://a.com")
            cmd2 = daemon.make_command("navigate", url="https://b.com")
            r1, r2 = await asyncio.gather(
                daemon.send(cmd1, context_id="user"),
                daemon.send(cmd2, context_id="user"),
            )

            assert r1.data == "first", "demux failed — cmd1 got cmd2's response"
            assert r2.data == "second"
            await stub_task
    finally:
        await daemon.stop()


@pytest.mark.asyncio
async def test_unsupported_action_rejected() -> None:
    """Daemon refuses v0.6.x actions before going to the wire."""
    port = _free_port()
    daemon = await _start_daemon(port)
    try:
        async with _stub_extension_ws(port, context_id="user") as _ws:
            await asyncio.sleep(0.1)
            cmd = daemon.make_command("bind", workspace="bound:learnx")
            with pytest.raises(ActionNotSupportedError):
                await daemon.send(cmd, context_id="user")
    finally:
        await daemon.stop()


@pytest.mark.asyncio
async def test_no_extension_connected_raises() -> None:
    port = _free_port()
    daemon = await _start_daemon(port)
    try:
        cmd = daemon.make_command("navigate", url="https://example.com")
        with pytest.raises(ControlDaemonError, match="no extension connected"):
            await daemon.send(cmd)
    finally:
        await daemon.stop()


@pytest.mark.asyncio
async def test_command_timeout_when_extension_silent() -> None:
    """If extension never responds, daemon raises CommandTimeoutError."""
    port = _free_port()
    daemon = ControlDaemon(port=port, command_timeout_s=0.3)
    await daemon.start()
    await asyncio.sleep(0.05)
    try:
        async with _stub_extension_ws(port, context_id="user") as ws:
            await asyncio.sleep(0.1)

            async def silent_drain() -> None:
                # Read but never respond.
                with contextlib.suppress(Exception):
                    await ws.recv()

            drain_task = asyncio.create_task(silent_drain())

            cmd = daemon.make_command("navigate", url="https://example.com")
            with pytest.raises(CommandTimeoutError):
                await daemon.send(cmd, context_id="user")
            await drain_task
    finally:
        await daemon.stop()


@pytest.mark.asyncio
async def test_disconnect_cancels_pending_commands() -> None:
    """When extension disconnects mid-flight, in-flight Future raises."""
    port = _free_port()
    daemon = await _start_daemon(port)
    try:
        ws_holder: list = []

        async def hold_open() -> None:
            import websockets  # type: ignore[import-not-found]

            ws = await websockets.connect(f"ws://127.0.0.1:{port}/ext")
            ws_holder.append(ws)
            await ws.send(
                json.dumps(
                    {
                        "type": "hello",
                        "contextId": "user",
                        "version": "0.6.0",
                        "compatRange": "^0.6.0",
                    }
                )
            )
            # Drain the Command so we have a pending future, then drop.
            await ws.recv()
            await ws.close()

        hold_task = asyncio.create_task(hold_open())
        await asyncio.sleep(0.15)  # let stub connect + register

        cmd = daemon.make_command("navigate", url="https://example.com")
        send_task = asyncio.create_task(daemon.send(cmd, context_id="user"))

        # Wait for hold_open to complete (which closes the WS).
        await hold_task

        # send() should now raise — its pending future was cancelled by
        # the disconnect handler.
        with pytest.raises(ControlDaemonError):
            await asyncio.wait_for(send_task, timeout=1.0)
    finally:
        await daemon.stop()

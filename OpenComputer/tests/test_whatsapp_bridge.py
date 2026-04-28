"""Tests for the WhatsApp Baileys-bridge adapter (PR 6.2).

We deliberately do NOT spawn a real Node subprocess. Instead the bridge
HTTP API is mocked via ``httpx.MockTransport`` (in-process aiohttp would
work too — MockTransport is lighter). The supervisor's spawn path is
exercised through monkey-patches of ``subprocess.Popen`` so the
cross-platform kill machinery is verified without touching the OS.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from plugin_sdk.core import MessageEvent


def _load_adapter_mod():
    spec = importlib.util.spec_from_file_location(
        "whatsapp_bridge_adapter_pr6",
        Path(__file__).resolve().parent.parent
        / "extensions" / "whatsapp-bridge" / "adapter.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_supervisor_mod():
    # The supervisor is sibling-imported by the adapter at runtime; load
    # it directly here so tests can drive its functions in isolation.
    sup_path = (
        Path(__file__).resolve().parent.parent
        / "extensions" / "whatsapp-bridge" / "bridge_supervisor.py"
    )
    spec = importlib.util.spec_from_file_location(
        "bridge_supervisor", sup_path
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    # Register so the adapter can find it via "from bridge_supervisor import …"
    sys.modules["bridge_supervisor"] = mod
    spec.loader.exec_module(mod)
    return mod


def _make_adapter_with_mock_http(handler):
    """Build a WhatsAppBridgeAdapter and inject an httpx.MockTransport."""
    _load_supervisor_mod()  # register for the adapter's sibling import
    mod = _load_adapter_mod()
    a = mod.WhatsAppBridgeAdapter(
        config={
            "host": "127.0.0.1",
            "port": 3001,
            "auth_dir": "/tmp/oc-wa-bridge-test",
            "bridge_dir": "/tmp/oc-wa-bridge-test/bridge",
        }
    )
    a._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url=a._base_url,
    )
    return a, mod


# ---------------------------------------------------------------------------
# _kill_port_process — POSIX shape
# ---------------------------------------------------------------------------


class TestKillPortProcessPosix:
    def test_lsof_pids_killed(self) -> None:
        sup = _load_supervisor_mod()
        kills: list[int] = []

        def fake_runner(argv: list[str]) -> Any:
            assert argv[0] == "lsof"
            assert argv[1] == "-ti"
            assert argv[2] == "tcp:3001"
            return SimpleNamespace(stdout="1234\n5678\n", stderr="", returncode=0)

        with (
            patch("sys.platform", "linux"),
            patch("os.kill", side_effect=lambda pid, sig: kills.append(pid)),
        ):
            # Reload the supervisor module so _IS_WINDOWS picks up linux.
            # Simpler: just call with platform mocked at module level.
            sup._IS_WINDOWS = False
            pids = sup._kill_port_process(3001, runner=fake_runner)
        assert pids == [1234, 5678]
        assert kills == [1234, 5678]

    def test_no_listener_returns_empty(self) -> None:
        sup = _load_supervisor_mod()

        def fake_runner(argv: list[str]) -> Any:
            return SimpleNamespace(stdout="", stderr="", returncode=1)

        sup._IS_WINDOWS = False
        with patch("os.kill") as kill_mock:
            pids = sup._kill_port_process(3001, runner=fake_runner)
        assert pids == []
        kill_mock.assert_not_called()

    def test_lsof_missing_no_crash(self) -> None:
        sup = _load_supervisor_mod()

        def fake_runner(argv: list[str]) -> Any:
            raise FileNotFoundError("lsof: not found")

        sup._IS_WINDOWS = False
        pids = sup._kill_port_process(3001, runner=fake_runner)
        assert pids == []


class TestKillPortProcessWindows:
    def test_netstat_taskkill_path(self) -> None:
        sup = _load_supervisor_mod()
        called: list[list[str]] = []

        def fake_runner(argv: list[str]) -> Any:
            called.append(argv)
            if argv[0] == "netstat":
                return SimpleNamespace(
                    stdout=(
                        "  Proto  Local           Foreign  State     PID\n"
                        "  TCP    127.0.0.1:3001  0.0.0.0:0  LISTENING  4242\n"
                        "  TCP    127.0.0.1:80    0.0.0.0:0  LISTENING  100\n"
                    ),
                    returncode=0,
                )
            # taskkill
            return SimpleNamespace(stdout="", returncode=0)

        sup._IS_WINDOWS = True
        try:
            pids = sup._kill_port_process(3001, runner=fake_runner)
        finally:
            sup._IS_WINDOWS = sys.platform == "win32"
        assert pids == [4242]
        # taskkill called with /F /T /PID 4242
        taskkill = [a for a in called if a[0] == "taskkill"]
        assert taskkill, "taskkill should be invoked"
        assert "4242" in taskkill[0]
        assert "/F" in taskkill[0]
        assert "/T" in taskkill[0]


# ---------------------------------------------------------------------------
# Spawn flags — verify cross-platform kwargs
# ---------------------------------------------------------------------------


class TestSpawnKwargs:
    def test_posix_uses_start_new_session(self) -> None:
        sup = _load_supervisor_mod()
        sv = sup.BridgeSupervisor(
            bridge_dir="/tmp", host="127.0.0.1", port=3001, auth_dir="/tmp"
        )
        sup._IS_WINDOWS = False
        kwargs = sv._spawn_kwargs()
        assert kwargs.get("start_new_session") is True
        assert "creationflags" not in kwargs

    def test_windows_uses_creationflags(self) -> None:
        sup = _load_supervisor_mod()
        sv = sup.BridgeSupervisor(
            bridge_dir="/tmp", host="127.0.0.1", port=3001, auth_dir="/tmp"
        )
        sup._IS_WINDOWS = True
        try:
            kwargs = sv._spawn_kwargs()
        finally:
            sup._IS_WINDOWS = sys.platform == "win32"
        assert "creationflags" in kwargs
        # Defaults to 0 on non-windows where the constant doesn't exist;
        # at minimum the key is present so the platform branch fires.
        assert "start_new_session" not in kwargs


# ---------------------------------------------------------------------------
# Send — HTTP request shape via MockTransport
# ---------------------------------------------------------------------------


class TestSend:
    @pytest.mark.asyncio
    async def test_send_posts_to_bridge_send_endpoint(self) -> None:
        seen: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            seen.append(req)
            assert req.method == "POST"
            assert req.url.path == "/send"
            return httpx.Response(200, json={"id": "msg-001"})

        a, _ = _make_adapter_with_mock_http(handler)
        try:
            res = await a.send("919876543210@s.whatsapp.net", "hello world")
            assert res.success is True
            assert res.message_id == "msg-001"
            # body is {"to": ..., "text": ...}
            assert seen
            body = seen[0].read().decode() or ""
            assert "919876543210" in body
            assert "hello world" in body
        finally:
            await a._client.aclose()

    @pytest.mark.asyncio
    async def test_send_4xx_returns_error_result(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(400, text="bad request body")

        a, _ = _make_adapter_with_mock_http(handler)
        try:
            res = await a.send("chat", "x")
            assert res.success is False
            assert "400" in (res.error or "")
        finally:
            await a._client.aclose()

    @pytest.mark.asyncio
    async def test_send_records_id_for_echo_suppression(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"id": "echo-123"})

        a, _ = _make_adapter_with_mock_http(handler)
        try:
            await a.send("chat", "x")
            assert "echo-123" in a._recently_sent_ids
        finally:
            await a._client.aclose()


# ---------------------------------------------------------------------------
# Inbound — envelope handling + echo suppression
# ---------------------------------------------------------------------------


class TestInbound:
    @pytest.mark.asyncio
    async def test_inbound_envelope_dispatches_message_event(self) -> None:
        a, _ = _make_adapter_with_mock_http(
            lambda req: httpx.Response(200, json=[])
        )
        try:
            captured: list[MessageEvent] = []

            async def _handler(event: MessageEvent) -> None:
                captured.append(event)

            a.set_message_handler(lambda ev: _handler(ev))  # type: ignore[arg-type]
            # We need set_message_handler's signature: it expects an
            # async callable returning str|None. Use a real coroutine.

            async def _handler2(event: MessageEvent) -> None:
                captured.append(event)
                return None

            a.set_message_handler(_handler2)  # type: ignore[arg-type]

            await a._handle_inbound_envelope(
                {
                    "id": "wa-001",
                    "chat": "919876@s.whatsapp.net",
                    "sender": "919876@s.whatsapp.net",
                    "fromMe": False,
                    "text": "ping",
                    "timestamp": 1700000000.0,
                }
            )
            assert captured
            ev = captured[0]
            assert ev.text == "ping"
            assert ev.chat_id == "919876@s.whatsapp.net"
            assert ev.metadata["message_id"] == "wa-001"
            assert ev.metadata["via"] == "bridge"
        finally:
            await a._client.aclose()

    @pytest.mark.asyncio
    async def test_inbound_echo_suppressed(self) -> None:
        a, _ = _make_adapter_with_mock_http(
            lambda req: httpx.Response(200, json=[])
        )
        try:
            captured: list[MessageEvent] = []

            async def _handler(event: MessageEvent) -> None:
                captured.append(event)

            a.set_message_handler(_handler)  # type: ignore[arg-type]
            a._recently_sent_ids.add("wa-self")
            await a._handle_inbound_envelope(
                {
                    "id": "wa-self",
                    "chat": "919876@s.whatsapp.net",
                    "fromMe": False,
                    "text": "echo",
                    "timestamp": 1700000000.0,
                }
            )
            assert not captured, "echo should be suppressed"
            # And the id is consumed.
            assert "wa-self" not in a._recently_sent_ids
        finally:
            await a._client.aclose()

    @pytest.mark.asyncio
    async def test_inbound_from_me_dropped(self) -> None:
        a, _ = _make_adapter_with_mock_http(
            lambda req: httpx.Response(200, json=[])
        )
        try:
            captured: list[MessageEvent] = []

            async def _handler(event: MessageEvent) -> None:
                captured.append(event)

            a.set_message_handler(_handler)  # type: ignore[arg-type]
            await a._handle_inbound_envelope(
                {
                    "id": "wa-x",
                    "chat": "919876@s.whatsapp.net",
                    "fromMe": True,
                    "text": "self-typed",
                    "timestamp": 1700000000.0,
                }
            )
            assert not captured
        finally:
            await a._client.aclose()


# ---------------------------------------------------------------------------
# QR-code emission as system event
# ---------------------------------------------------------------------------


class TestQrEmission:
    @pytest.mark.asyncio
    async def test_qr_dispatched_as_system_event(self) -> None:
        a, mod = _make_adapter_with_mock_http(
            lambda req: httpx.Response(200, json=[])
        )
        try:
            captured: list[MessageEvent] = []

            async def _handler(event: MessageEvent) -> None:
                captured.append(event)

            a.set_message_handler(_handler)  # type: ignore[arg-type]
            await a._dispatch_qr_event("XXX-base64-payload-XXX")
            assert captured, "QR should produce a MessageEvent"
            ev = captured[0]
            assert ev.metadata.get("kind") == "whatsapp_bridge_qr"
            assert ev.metadata.get("system") is True
            assert ev.chat_id == "__system__"
            assert "XXX-base64-payload-XXX" in ev.text
        finally:
            await a._client.aclose()

    @pytest.mark.asyncio
    async def test_empty_qr_skipped(self) -> None:
        a, _ = _make_adapter_with_mock_http(
            lambda req: httpx.Response(200, json=[])
        )
        try:
            captured: list[MessageEvent] = []

            async def _handler(event: MessageEvent) -> None:
                captured.append(event)

            a.set_message_handler(_handler)  # type: ignore[arg-type]
            await a._dispatch_qr_event("")
            assert not captured
        finally:
            await a._client.aclose()


# ---------------------------------------------------------------------------
# Connect — verifies HTTP health + supervisor lifecycle (mocked)
# ---------------------------------------------------------------------------


class TestConnectLifecycle:
    @pytest.mark.asyncio
    async def test_connect_polls_health_until_ok(self) -> None:
        # Bridge supervisor is mocked so no real Node is spawned. Health
        # returns 503 once then 200.
        call_count = {"n": 0}

        def handler(req: httpx.Request) -> httpx.Response:
            if req.url.path == "/health":
                call_count["n"] += 1
                if call_count["n"] == 1:
                    return httpx.Response(503, text="warming up")
                return httpx.Response(200, json={"ok": True, "ready": True})
            if req.url.path == "/messages":
                return httpx.Response(200, json=[])
            return httpx.Response(404)

        a, mod = _make_adapter_with_mock_http(handler)
        try:
            # Replace BridgeSupervisor with a stub so spawn() is a no-op.
            class _StubSup:
                def __init__(self, **_kw: Any) -> None:
                    self._proc = None

                def spawn(self) -> Any:
                    return SimpleNamespace(pid=1, poll=lambda: None)

                def is_alive(self) -> bool:
                    return True

                def terminate(self, **_kw: Any) -> None:
                    return None

                def append_stdout(self, *_a: Any) -> None:
                    return None

            with patch.object(mod, "BridgeSupervisor", _StubSup):
                ok = await a.connect()
            # Cancel poll loop quickly so the test finishes
            a._stop_event.set()
            if a._poll_task is not None:
                a._poll_task.cancel()
                try:
                    await a._poll_task
                except (asyncio.CancelledError, Exception):
                    pass
            assert ok is True
            assert call_count["n"] >= 2
        finally:
            await a._client.aclose()

    @pytest.mark.asyncio
    async def test_disconnect_terminates_supervisor(self) -> None:
        a, mod = _make_adapter_with_mock_http(
            lambda req: httpx.Response(200, json=[])
        )
        try:
            terminated = {"called": False}

            class _StubSup:
                def __init__(self, **_kw: Any) -> None:
                    self._proc = None

                def spawn(self) -> Any:
                    return SimpleNamespace(pid=1, poll=lambda: None)

                def is_alive(self) -> bool:
                    return True

                def terminate(self, **_kw: Any) -> None:
                    terminated["called"] = True

                def append_stdout(self, *_a: Any) -> None:
                    return None

            a._supervisor = _StubSup()
            await a.disconnect()
            assert terminated["called"] is True
            assert a._supervisor is None
        finally:
            # _client was already closed by disconnect
            pass


# ---------------------------------------------------------------------------
# Plugin manifest validates + discovery picks it up
# ---------------------------------------------------------------------------


class TestPluginDiscovery:
    def test_manifest_has_no_capabilities_field(self) -> None:
        # Audit C3 — PluginManifestSchema rejects unknown fields.
        import json

        manifest = json.loads(
            (
                Path(__file__).resolve().parent.parent
                / "extensions" / "whatsapp-bridge" / "plugin.json"
            ).read_text()
        )
        assert "capabilities" not in manifest, (
            "capabilities field is rejected by PluginManifestSchema"
        )
        # And the manifest still validates.
        from opencomputer.plugins.manifest_validator import validate_manifest
        schema, err = validate_manifest(manifest)
        assert schema is not None, f"manifest invalid: {err}"

    def test_discovery_finds_bridge_plugin(self) -> None:
        from opencomputer.plugins.discovery import discover

        candidates = discover(
            [Path(__file__).resolve().parent.parent / "extensions"],
            force_rescan=True,
        )
        ids = {c.manifest.id for c in candidates}
        assert "whatsapp-bridge" in ids

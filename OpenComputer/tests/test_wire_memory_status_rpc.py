"""Tier-C+ tests for the ``memory.status`` wire RPC.

Closes the "panel sees nothing on a fresh connect" gap: a wire client
that just opened a WS connection has no memory event history (no
ring-buffer for global broadcasts), so the panel would show nothing
until the user wrote to memory. This RPC lets the client seed the panel
with current MEMORY.md / USER.md cap status from the first frame.

Coverage:

* **Protocol surface** — constant + typed schemas + METHOD_SCHEMAS entry +
  hello-handshake advertises the new method/event.
* **Helper** — ``WireServer._collect_memory_status`` reads files via
  MemoryManager + computes CapStatus for each. Edge cases: missing files,
  unreadable files, no MemoryManager.
* **End-to-end RPC** — real WireServer, real WS client, real
  MemoryManager → response shape matches schema, payload values match
  on-disk state.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

# ─── Protocol surface ──────────────────────────────────────────────


class TestMemoryStatusProtocol:
    """Pin the wire-protocol surface for memory.status."""

    def test_method_constant(self) -> None:
        from opencomputer.gateway.protocol import METHOD_MEMORY_STATUS

        assert METHOD_MEMORY_STATUS == "memory.status"

    def test_method_in_protocol_all(self) -> None:
        from opencomputer.gateway import protocol

        assert "METHOD_MEMORY_STATUS" in protocol.__all__

    def test_method_in_protocol_v2_all(self) -> None:
        from opencomputer.gateway import protocol_v2

        for name in (
            "METHOD_MEMORY_STATUS",
            "MemoryStatusParams",
            "MemoryStatusEntry",
            "MemoryStatusResult",
        ):
            assert name in protocol_v2.__all__, f"missing __all__: {name}"

    def test_method_schemas_registry(self) -> None:
        from opencomputer.gateway.protocol import METHOD_MEMORY_STATUS
        from opencomputer.gateway.protocol_v2 import (
            METHOD_SCHEMAS,
            MemoryStatusParams,
            MemoryStatusResult,
        )

        params_cls, result_cls = METHOD_SCHEMAS[METHOD_MEMORY_STATUS]
        assert params_cls is MemoryStatusParams
        assert result_cls is MemoryStatusResult

    def test_entry_round_trip(self) -> None:
        from opencomputer.gateway.protocol_v2 import (
            MemoryStatusEntry,
            MemoryStatusResult,
        )

        e1 = MemoryStatusEntry(
            target="MEMORY.md",
            content_size=3480,
            cap_limit=4000,
            pct=0.87,
            paragraph_count=5,
        )
        e2 = MemoryStatusEntry(
            target="USER.md",
            content_size=1785,
            cap_limit=2000,
            pct=0.8925,
            paragraph_count=3,
        )
        result = MemoryStatusResult(entries=(e1, e2))
        s = result.model_dump_json()
        restored = MemoryStatusResult.model_validate_json(s)
        assert restored == result
        assert len(restored.entries) == 2

    def test_entry_rejects_unknown_field(self) -> None:
        from opencomputer.gateway.protocol_v2 import MemoryStatusEntry

        with pytest.raises(Exception):  # pydantic.ValidationError
            MemoryStatusEntry(
                target="MEMORY.md",
                content_size=100,
                cap_limit=4000,
                pct=0.025,
                paragraph_count=1,
                bogus="surprise",
            )

    def test_params_accepts_no_args(self) -> None:
        from opencomputer.gateway.protocol_v2 import MemoryStatusParams

        # Empty params class — should construct without args.
        p = MemoryStatusParams()
        assert p.model_dump() == {}


# ─── _collect_memory_status helper unit tests ──────────────────────


class TestCollectMemoryStatusHelper:
    """The static helper handles every failure mode without raising."""

    def test_loop_without_memory_returns_empty(self) -> None:
        from opencomputer.gateway.wire_server import WireServer

        loop = MagicMock(spec=[])  # spec=[] → no attrs at all
        # getattr(loop, "memory", None) returns None on spec=[] mocks.
        result = WireServer._collect_memory_status(loop)
        assert result == []

    def test_missing_files_report_zero(self, tmp_path: Path) -> None:
        from opencomputer.agent.memory import MemoryManager
        from opencomputer.gateway.wire_server import WireServer

        mm = MemoryManager(
            declarative_path=tmp_path / "MEMORY.md",
            user_path=tmp_path / "USER.md",
            skills_path=tmp_path / "skills",
        )
        loop = MagicMock()
        loop.memory = mm

        result = WireServer._collect_memory_status(loop)
        assert len(result) == 2
        # Sorted alphabetically: MEMORY.md before USER.md
        assert result[0]["target"] == "MEMORY.md"
        assert result[0]["content_size"] == 0
        assert result[0]["cap_limit"] == 4000
        assert result[0]["pct"] == 0.0
        assert result[1]["target"] == "USER.md"
        assert result[1]["cap_limit"] == 2000

    def test_populated_files_reflect_disk_state(self, tmp_path: Path) -> None:
        from opencomputer.agent.memory import MemoryManager
        from opencomputer.gateway.wire_server import WireServer

        memory_text = "rule one\n\nrule two with longer content\n"
        user_text = "preference one"
        (tmp_path / "MEMORY.md").write_text(memory_text)
        (tmp_path / "USER.md").write_text(user_text)

        mm = MemoryManager(
            declarative_path=tmp_path / "MEMORY.md",
            user_path=tmp_path / "USER.md",
            skills_path=tmp_path / "skills",
        )
        loop = MagicMock()
        loop.memory = mm

        result = WireServer._collect_memory_status(loop)
        memory_entry = next(e for e in result if e["target"] == "MEMORY.md")
        user_entry = next(e for e in result if e["target"] == "USER.md")
        assert memory_entry["content_size"] == len(memory_text)
        assert user_entry["content_size"] == len(user_text)
        assert memory_entry["paragraph_count"] >= 1
        assert 0.0 < memory_entry["pct"] < 1.0

    def test_unreadable_file_is_omitted_not_raising(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Permission errors on one file omit that entry; the other still
        reports. Helper never raises."""
        from opencomputer.agent.memory import MemoryManager
        from opencomputer.gateway.wire_server import WireServer

        memory_path = tmp_path / "MEMORY.md"
        user_path = tmp_path / "USER.md"
        memory_path.write_text("ok content")
        user_path.write_text("user content")

        mm = MemoryManager(
            declarative_path=memory_path,
            user_path=user_path,
            skills_path=tmp_path / "skills",
        )
        loop = MagicMock()
        loop.memory = mm

        # Force read failure on USER.md only by monkeypatching its read_text.
        original_read = Path.read_text

        def patched_read(self: Path, *a: Any, **kw: Any) -> str:
            if self == user_path:
                raise PermissionError("denied")
            return original_read(self, *a, **kw)

        monkeypatch.setattr(Path, "read_text", patched_read)

        result = WireServer._collect_memory_status(loop)
        # USER.md omitted, MEMORY.md still present.
        targets = {e["target"] for e in result}
        assert "MEMORY.md" in targets
        assert "USER.md" not in targets


# ─── End-to-end RPC over a real WS ─────────────────────────────────


@contextlib.asynccontextmanager
async def _wire_server_with_memory(tmp_path: Path):
    """Spin up a real WireServer wired to a real MemoryManager."""
    from opencomputer.agent.loop import AgentLoop
    from opencomputer.agent.memory import MemoryManager
    from opencomputer.gateway.wire_server import WireServer

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    # Real MemoryManager pointing at tmp_path so test files are isolated.
    mm = MemoryManager(
        declarative_path=tmp_path / "MEMORY.md",
        user_path=tmp_path / "USER.md",
        skills_path=tmp_path / "skills",
    )
    fake_loop = MagicMock(spec=AgentLoop)
    fake_loop.memory = mm
    server = WireServer(loop=fake_loop, host="127.0.0.1", port=port)
    await server.start()
    try:
        yield server, f"ws://127.0.0.1:{port}", mm
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_memory_status_rpc_returns_schema_compliant_payload(
    tmp_path: Path,
) -> None:
    """A real WS client calls memory.status and receives a payload that
    parses cleanly through the typed MemoryStatusResult schema."""
    import websockets

    (tmp_path / "MEMORY.md").write_text("first rule\n\nsecond rule\n")
    (tmp_path / "USER.md").write_text("preference\n")

    async with _wire_server_with_memory(tmp_path) as (_server, url, _mm):
        async with websockets.connect(url) as client:
            req = {"type": "req", "id": "rpc-1", "method": "memory.status", "params": {}}
            await client.send(json.dumps(req))
            raw = await asyncio.wait_for(client.recv(), timeout=2.0)
            msg = json.loads(raw)

            assert msg["type"] == "res"
            assert msg["id"] == "rpc-1"
            assert msg["ok"] is True
            payload = msg["payload"]
            assert "entries" in payload
            entries = payload["entries"]
            # Sorted: MEMORY.md before USER.md
            assert entries[0]["target"] == "MEMORY.md"
            assert entries[1]["target"] == "USER.md"
            # Validates against the typed schema.
            from opencomputer.gateway.protocol_v2 import MemoryStatusResult

            MemoryStatusResult.model_validate(payload)


@pytest.mark.asyncio
async def test_memory_status_rpc_with_no_memory_manager_returns_empty(
    tmp_path: Path,
) -> None:
    """Loop without a memory attribute → empty entries (not an error)."""
    import websockets

    from opencomputer.agent.loop import AgentLoop
    from opencomputer.gateway.wire_server import WireServer

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    fake_loop = MagicMock(spec=AgentLoop)
    fake_loop.memory = None
    server = WireServer(loop=fake_loop, host="127.0.0.1", port=port)
    await server.start()
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}") as client:
            req = {"type": "req", "id": "rpc-2", "method": "memory.status", "params": {}}
            await client.send(json.dumps(req))
            raw = await asyncio.wait_for(client.recv(), timeout=2.0)
            msg = json.loads(raw)

            assert msg["ok"] is True
            assert msg["payload"]["entries"] == []
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_hello_handshake_advertises_memory_status(tmp_path: Path) -> None:
    """The hello-response method/event lists must include the new wire surface
    so capability-detecting clients see it."""
    import websockets

    async with _wire_server_with_memory(tmp_path) as (_server, url, _mm):
        async with websockets.connect(url) as client:
            req = {"type": "req", "id": "h-1", "method": "hello", "params": {}}
            await client.send(json.dumps(req))
            raw = await asyncio.wait_for(client.recv(), timeout=2.0)
            msg = json.loads(raw)

            assert msg["ok"] is True
            assert "memory.status" in msg["payload"]["methods"]
            assert "memory.write" in msg["payload"]["events"]


@pytest.mark.asyncio
async def test_request_without_type_field_is_rejected(tmp_path: Path) -> None:
    """Server MUST reject requests missing ``type: "req"`` so a wire-shape
    mismatch in any client (TS, Python, IDE bridge) fails loudly with a
    diagnosable error rather than silently no-op-ing.

    Pre-2026-05-10 the TS ``OCWireClient`` (gatewayClient.ts) omitted
    this discriminator, leaving every TUI RPC silently broken — the WS
    opened so the "connected" indicator lit up, but every subsequent
    call errored. The fix added ``type: "req"`` to the TS send + this
    test pins the server-side contract so a future relaxation of the
    server check would surface in CI rather than in user-facing breakage.
    """
    import websockets

    async with _wire_server_with_memory(tmp_path) as (_server, url, _mm):
        async with websockets.connect(url) as client:
            # Deliberately omit type=req — the bug shape from gatewayClient.ts
            # before the 2026-05-10 fix.
            bad_req = {"id": "bad-1", "method": "hello", "params": {}}
            await client.send(json.dumps(bad_req))
            raw = await asyncio.wait_for(client.recv(), timeout=2.0)
            msg = json.loads(raw)

            assert msg["type"] == "res"
            assert msg["ok"] is False
            assert "expected type=req" in (msg.get("error") or "")

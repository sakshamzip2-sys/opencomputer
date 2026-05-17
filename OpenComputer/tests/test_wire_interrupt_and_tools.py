"""Wire RPCs ``session.interrupt`` + ``tools.list`` — TUI-parity M1 batch 7.

Spec: ``docs/superpowers/specs/2026-05-17-tui-parity/TUI.md``.

* ``session.interrupt`` — signal a mid-run turn to cancel. Sets the
  steer registry's per-session cancel Event, which the agent loop
  watches between/within turns. Powers a TUI "stop" affordance.
* ``tools.list`` — every registered tool's name + description. Powers
  a tools overlay / capability inspector.

Coverage mirrors ``test_wire_session_lifecycle.py``: protocol surface +
graceful helper units + end-to-end RPC over a real WS.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
from unittest.mock import MagicMock

import pytest

# ─── Protocol surface ──────────────────────────────────────────────


class TestInterruptToolsProtocol:
    def test_method_constants(self) -> None:
        from opencomputer.gateway.protocol import (
            METHOD_SESSION_INTERRUPT,
            METHOD_TOOLS_LIST,
        )

        assert METHOD_SESSION_INTERRUPT == "session.interrupt"
        assert METHOD_TOOLS_LIST == "tools.list"

    def test_methods_in_protocol_all(self) -> None:
        from opencomputer.gateway import protocol

        assert "METHOD_SESSION_INTERRUPT" in protocol.__all__
        assert "METHOD_TOOLS_LIST" in protocol.__all__

    def test_schemas_in_protocol_v2_all(self) -> None:
        from opencomputer.gateway import protocol_v2

        for name in (
            "METHOD_SESSION_INTERRUPT",
            "METHOD_TOOLS_LIST",
            "SessionInterruptParams",
            "SessionInterruptResult",
            "ToolsListParams",
            "ToolsListResult",
            "ToolInfo",
        ):
            assert name in protocol_v2.__all__, f"missing __all__: {name}"

    def test_method_schemas_registry(self) -> None:
        from opencomputer.gateway.protocol import (
            METHOD_SESSION_INTERRUPT,
            METHOD_TOOLS_LIST,
        )
        from opencomputer.gateway.protocol_v2 import (
            METHOD_SCHEMAS,
            SessionInterruptParams,
            SessionInterruptResult,
            ToolsListParams,
            ToolsListResult,
        )

        assert METHOD_SCHEMAS[METHOD_SESSION_INTERRUPT] == (
            SessionInterruptParams,
            SessionInterruptResult,
        )
        assert METHOD_SCHEMAS[METHOD_TOOLS_LIST] == (
            ToolsListParams,
            ToolsListResult,
        )

    def test_result_round_trips(self) -> None:
        from opencomputer.gateway.protocol_v2 import (
            SessionInterruptResult,
            ToolInfo,
            ToolsListResult,
        )

        i = SessionInterruptResult(session_id="s", ok=True)
        assert SessionInterruptResult.model_validate_json(i.model_dump_json()) == i
        t = ToolsListResult(
            tools=(ToolInfo(name="Edit", description="Edit a file"),)
        )
        assert ToolsListResult.model_validate_json(t.model_dump_json()) == t

    def test_params_reject_unknown_field(self) -> None:
        from opencomputer.gateway.protocol_v2 import SessionInterruptParams

        with pytest.raises(Exception):  # pydantic.ValidationError
            SessionInterruptParams(session_id="s", bogus="x")


# ─── helper unit tests ─────────────────────────────────────────────


class TestCollectToolsHelper:
    def test_returns_list_of_name_description(self) -> None:
        from opencomputer.gateway.wire_server import WireServer

        out = WireServer._collect_tools()
        assert isinstance(out, list)
        for entry in out:
            assert set(entry.keys()) == {"name", "description"}
            assert isinstance(entry["name"], str)
        json.dumps(out)

    def test_registry_failure_degrades_to_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the tool registry blows up, the helper returns [] not a raise."""
        import opencomputer.tools.registry as reg_mod
        from opencomputer.gateway.wire_server import WireServer

        def boom(*_a: object, **_kw: object) -> list:
            raise RuntimeError("registry exploded")

        monkeypatch.setattr(reg_mod.registry, "tool_summaries", boom)
        assert WireServer._collect_tools() == []


# ─── End-to-end RPC over a real WS ─────────────────────────────────


@contextlib.asynccontextmanager
async def _wire_server():
    from opencomputer.agent.loop import AgentLoop
    from opencomputer.gateway.wire_server import WireServer

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    fake_loop = MagicMock(spec=AgentLoop)
    server = WireServer(loop=fake_loop, host="127.0.0.1", port=port)
    await server.start()
    try:
        yield f"ws://127.0.0.1:{port}"
    finally:
        await server.stop()


async def _rpc(url: str, method: str, params: dict) -> dict:
    import websockets

    async with websockets.connect(url) as client:
        req = {"type": "req", "id": "t-1", "method": method, "params": params}
        await client.send(json.dumps(req))
        raw = await asyncio.wait_for(client.recv(), timeout=2.0)
        return json.loads(raw)


@pytest.mark.asyncio
async def test_session_interrupt_rpc_sets_cancel_event() -> None:
    from opencomputer.agent.steer import default_registry

    sid = "interrupt-target-1"
    async with _wire_server() as url:
        msg = await _rpc(url, "session.interrupt", {"session_id": sid})
        assert msg["ok"] is True
        assert msg["payload"]["ok"] is True
        assert msg["payload"]["session_id"] == sid

        from opencomputer.gateway.protocol_v2 import SessionInterruptResult

        SessionInterruptResult.model_validate(msg["payload"])
        # The cancel signal genuinely landed — the agent loop watches this.
        assert default_registry.cancel_event(sid).is_set()


@pytest.mark.asyncio
async def test_session_interrupt_missing_param_is_error() -> None:
    async with _wire_server() as url:
        msg = await _rpc(url, "session.interrupt", {})
        assert msg["ok"] is False
        assert "session_id" in (msg.get("error") or "")


@pytest.mark.asyncio
async def test_tools_list_rpc_returns_schema_compliant_payload() -> None:
    async with _wire_server() as url:
        msg = await _rpc(url, "tools.list", {})
        assert msg["ok"] is True
        assert isinstance(msg["payload"]["tools"], list)

        from opencomputer.gateway.protocol_v2 import ToolsListResult

        ToolsListResult.model_validate(msg["payload"])


@pytest.mark.asyncio
async def test_hello_handshake_advertises_batch7() -> None:
    async with _wire_server() as url:
        msg = await _rpc(url, "hello", {})
        methods = msg["payload"]["methods"]
        assert "session.interrupt" in methods
        assert "tools.list" in methods

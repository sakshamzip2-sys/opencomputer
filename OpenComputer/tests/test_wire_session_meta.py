"""Wire RPCs for session metadata — ``session.rename`` + ``session.usage``.

TUI-parity Milestone 1, batch 4 (spec:
``docs/superpowers/specs/2026-05-17-tui-parity/TUI.md``).

* ``session.rename`` — set a session's title. Powers in-picker rename.
* ``session.usage`` — per-session token / cache / cost totals. Powers a
  usage panel + status-bar cost readout.

Coverage mirrors ``test_wire_session_lifecycle.py``: protocol surface +
graceful helper units + end-to-end RPC over a real WS with a real
SessionDB.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ─── Protocol surface ──────────────────────────────────────────────


class TestSessionMetaProtocol:
    def test_method_constants(self) -> None:
        from opencomputer.gateway.protocol import (
            METHOD_SESSION_RENAME,
            METHOD_SESSION_USAGE,
        )

        assert METHOD_SESSION_RENAME == "session.rename"
        assert METHOD_SESSION_USAGE == "session.usage"

    def test_methods_in_protocol_all(self) -> None:
        from opencomputer.gateway import protocol

        assert "METHOD_SESSION_RENAME" in protocol.__all__
        assert "METHOD_SESSION_USAGE" in protocol.__all__

    def test_schemas_in_protocol_v2_all(self) -> None:
        from opencomputer.gateway import protocol_v2

        for name in (
            "METHOD_SESSION_RENAME",
            "METHOD_SESSION_USAGE",
            "SessionRenameParams",
            "SessionRenameResult",
            "SessionUsageParams",
            "SessionUsageResult",
        ):
            assert name in protocol_v2.__all__, f"missing __all__: {name}"

    def test_method_schemas_registry(self) -> None:
        from opencomputer.gateway.protocol import (
            METHOD_SESSION_RENAME,
            METHOD_SESSION_USAGE,
        )
        from opencomputer.gateway.protocol_v2 import (
            METHOD_SCHEMAS,
            SessionRenameParams,
            SessionRenameResult,
            SessionUsageParams,
            SessionUsageResult,
        )

        assert METHOD_SCHEMAS[METHOD_SESSION_RENAME] == (
            SessionRenameParams,
            SessionRenameResult,
        )
        assert METHOD_SCHEMAS[METHOD_SESSION_USAGE] == (
            SessionUsageParams,
            SessionUsageResult,
        )

    def test_result_round_trips(self) -> None:
        from opencomputer.gateway.protocol_v2 import (
            SessionRenameResult,
            SessionUsageResult,
        )

        r = SessionRenameResult(session_id="s", title="New Title", ok=True)
        assert SessionRenameResult.model_validate_json(r.model_dump_json()) == r
        u = SessionUsageResult(
            session_id="s",
            found=True,
            model="m",
            input_tokens=100,
            output_tokens=50,
        )
        assert SessionUsageResult.model_validate_json(u.model_dump_json()) == u

    def test_params_reject_unknown_field(self) -> None:
        from opencomputer.gateway.protocol_v2 import SessionRenameParams

        with pytest.raises(Exception):  # pydantic.ValidationError
            SessionRenameParams(session_id="s", title="t", bogus="x")


# ─── helper unit tests ─────────────────────────────────────────────


class TestCollectSessionUsageHelper:
    def test_loop_without_db_returns_none(self) -> None:
        from opencomputer.gateway.wire_server import WireServer

        loop = MagicMock(spec=[])
        assert WireServer._collect_session_usage(loop, "any") is None

    def test_unknown_session_returns_none(self, tmp_path: Path) -> None:
        from opencomputer.agent.state import SessionDB
        from opencomputer.gateway.wire_server import WireServer

        db = SessionDB(tmp_path / "sessions.db")
        loop = MagicMock()
        loop.db = db
        assert WireServer._collect_session_usage(loop, "ghost") is None

    def test_known_session_returns_usage(self, tmp_path: Path) -> None:
        from opencomputer.agent.state import SessionDB
        from opencomputer.gateway.wire_server import WireServer

        db = SessionDB(tmp_path / "sessions.db")
        sid = "sess-usage-0001"
        db.create_session(session_id=sid, platform="cli", model="m")
        loop = MagicMock()
        loop.db = db
        out = WireServer._collect_session_usage(loop, sid)
        assert out is not None
        assert out["session_id"] == sid
        assert out["found"] is True
        # Token fields present + JSON-safe even for a brand-new session.
        for k in ("input_tokens", "output_tokens", "compactions_count"):
            assert k in out
        json.dumps(out)


# ─── End-to-end RPC over a real WS ─────────────────────────────────


@contextlib.asynccontextmanager
async def _wire_server(tmp_path: Path):
    from opencomputer.agent.loop import AgentLoop
    from opencomputer.agent.state import SessionDB
    from opencomputer.gateway.wire_server import WireServer

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    db = SessionDB(tmp_path / "sessions.db")
    fake_loop = MagicMock(spec=AgentLoop)
    fake_loop.db = db
    server = WireServer(loop=fake_loop, host="127.0.0.1", port=port)
    await server.start()
    try:
        yield f"ws://127.0.0.1:{port}", db
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
async def test_session_rename_rpc_persists(tmp_path: Path) -> None:
    async with _wire_server(tmp_path) as (url, db):
        sid = "sess-rename-1"
        db.create_session(session_id=sid, platform="cli", model="m")

        msg = await _rpc(
            url, "session.rename", {"session_id": sid, "title": "Renamed!"}
        )
        assert msg["ok"] is True
        assert msg["payload"]["ok"] is True
        assert msg["payload"]["title"] == "Renamed!"

        from opencomputer.gateway.protocol_v2 import SessionRenameResult

        SessionRenameResult.model_validate(msg["payload"])
        assert db.get_session_title(sid) == "Renamed!"


@pytest.mark.asyncio
async def test_session_rename_missing_title_is_error(tmp_path: Path) -> None:
    async with _wire_server(tmp_path) as (url, _db):
        msg = await _rpc(url, "session.rename", {"session_id": "s"})
        assert msg["ok"] is False
        assert "title" in (msg.get("error") or "")


@pytest.mark.asyncio
async def test_session_usage_rpc_returns_totals(tmp_path: Path) -> None:
    async with _wire_server(tmp_path) as (url, db):
        sid = "sess-usage-e2e"
        db.create_session(session_id=sid, platform="cli", model="m")

        msg = await _rpc(url, "session.usage", {"session_id": sid})
        assert msg["ok"] is True
        assert msg["payload"]["found"] is True
        assert msg["payload"]["session_id"] == sid

        from opencomputer.gateway.protocol_v2 import SessionUsageResult

        SessionUsageResult.model_validate(msg["payload"])


@pytest.mark.asyncio
async def test_session_usage_unknown_id_is_found_false(tmp_path: Path) -> None:
    async with _wire_server(tmp_path) as (url, _db):
        msg = await _rpc(url, "session.usage", {"session_id": "ghost"})
        # Unknown id is not an error — found=False.
        assert msg["ok"] is True
        assert msg["payload"]["found"] is False


@pytest.mark.asyncio
async def test_session_usage_missing_param_is_error(tmp_path: Path) -> None:
    async with _wire_server(tmp_path) as (url, _db):
        msg = await _rpc(url, "session.usage", {})
        assert msg["ok"] is False
        assert "session_id" in (msg.get("error") or "")


@pytest.mark.asyncio
async def test_hello_handshake_advertises_session_meta(tmp_path: Path) -> None:
    async with _wire_server(tmp_path) as (url, _db):
        msg = await _rpc(url, "hello", {})
        methods = msg["payload"]["methods"]
        assert "session.rename" in methods
        assert "session.usage" in methods

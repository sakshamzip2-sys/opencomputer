"""Wire RPCs ``checkpoints.list`` + ``checkpoints.delete`` — TUI-parity M1 batch 8.

Spec: ``docs/superpowers/specs/2026-05-17-tui-parity/TUI.md``.

* ``checkpoints.list`` — a session's prompt checkpoints (the message-history
  snapshots backing /checkpoint + /restore). Powers a rollback overlay.
* ``checkpoints.delete`` — prune a stale checkpoint from that overlay.

Coverage mirrors ``test_wire_session_lifecycle.py``: protocol surface +
graceful helper units + end-to-end RPC over a real WS with a real SessionDB.
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


class TestCheckpointsProtocol:
    def test_method_constants(self) -> None:
        from opencomputer.gateway.protocol import (
            METHOD_CHECKPOINTS_DELETE,
            METHOD_CHECKPOINTS_LIST,
        )

        assert METHOD_CHECKPOINTS_LIST == "checkpoints.list"
        assert METHOD_CHECKPOINTS_DELETE == "checkpoints.delete"

    def test_methods_in_protocol_all(self) -> None:
        from opencomputer.gateway import protocol

        assert "METHOD_CHECKPOINTS_LIST" in protocol.__all__
        assert "METHOD_CHECKPOINTS_DELETE" in protocol.__all__

    def test_schemas_in_protocol_v2_all(self) -> None:
        from opencomputer.gateway import protocol_v2

        for name in (
            "METHOD_CHECKPOINTS_LIST",
            "METHOD_CHECKPOINTS_DELETE",
            "CheckpointInfo",
            "CheckpointsListParams",
            "CheckpointsListResult",
            "CheckpointsDeleteParams",
            "CheckpointsDeleteResult",
        ):
            assert name in protocol_v2.__all__, f"missing __all__: {name}"

    def test_method_schemas_registry(self) -> None:
        from opencomputer.gateway.protocol import (
            METHOD_CHECKPOINTS_DELETE,
            METHOD_CHECKPOINTS_LIST,
        )
        from opencomputer.gateway.protocol_v2 import (
            METHOD_SCHEMAS,
            CheckpointsDeleteParams,
            CheckpointsDeleteResult,
            CheckpointsListParams,
            CheckpointsListResult,
        )

        assert METHOD_SCHEMAS[METHOD_CHECKPOINTS_LIST] == (
            CheckpointsListParams,
            CheckpointsListResult,
        )
        assert METHOD_SCHEMAS[METHOD_CHECKPOINTS_DELETE] == (
            CheckpointsDeleteParams,
            CheckpointsDeleteResult,
        )

    def test_result_round_trips(self) -> None:
        from opencomputer.gateway.protocol_v2 import (
            CheckpointInfo,
            CheckpointsDeleteResult,
            CheckpointsListResult,
        )

        c = CheckpointsListResult(
            checkpoints=(
                CheckpointInfo(
                    id="cp1",
                    session_id="s",
                    prompt_index=3,
                    label="before-Edit",
                    created_at=1700000000.0,
                    message_count=6,
                ),
            )
        )
        assert CheckpointsListResult.model_validate_json(c.model_dump_json()) == c
        d = CheckpointsDeleteResult(checkpoint_id="cp1", found=True)
        assert CheckpointsDeleteResult.model_validate_json(d.model_dump_json()) == d

    def test_params_reject_unknown_field(self) -> None:
        from opencomputer.gateway.protocol_v2 import CheckpointsListParams

        with pytest.raises(Exception):  # pydantic.ValidationError
            CheckpointsListParams(session_id="s", bogus="x")


# ─── helper unit tests ─────────────────────────────────────────────


class TestCollectCheckpointsHelper:
    def test_loop_without_db_returns_empty(self) -> None:
        from opencomputer.gateway.wire_server import WireServer

        loop = MagicMock(spec=[])
        assert WireServer._collect_checkpoints(loop, "any", 50) == []

    def test_unknown_session_returns_empty(self, tmp_path: Path) -> None:
        from opencomputer.agent.state import SessionDB
        from opencomputer.gateway.wire_server import WireServer

        db = SessionDB(tmp_path / "sessions.db")
        loop = MagicMock()
        loop.db = db
        assert WireServer._collect_checkpoints(loop, "ghost", 50) == []

    def test_populated_session_returns_checkpoints(self, tmp_path: Path) -> None:
        from opencomputer.agent.state import SessionDB
        from opencomputer.gateway.wire_server import WireServer

        db = SessionDB(tmp_path / "sessions.db")
        sid = "cp-session-1"
        db.create_session(session_id=sid, platform="cli", model="m")
        db.create_prompt_checkpoint(
            session_id=sid,
            prompt_index=1,
            messages=[{"role": "user", "content": "hi"}],
            label="first",
        )
        loop = MagicMock()
        loop.db = db
        out = WireServer._collect_checkpoints(loop, sid, 50)
        assert len(out) == 1
        assert out[0]["session_id"] == sid
        assert out[0]["label"] == "first"
        assert out[0]["message_count"] == 1
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


def _make_checkpoint(db, session_id: str, label: str) -> str:
    return db.create_prompt_checkpoint(
        session_id=session_id,
        prompt_index=1,
        messages=[{"role": "user", "content": "x"}],
        label=label,
    )


@pytest.mark.asyncio
async def test_checkpoints_list_rpc(tmp_path: Path) -> None:
    async with _wire_server(tmp_path) as (url, db):
        sid = "cp-e2e-list"
        db.create_session(session_id=sid, platform="cli", model="m")
        _make_checkpoint(db, sid, "cp-a")
        _make_checkpoint(db, sid, "cp-b")

        msg = await _rpc(url, "checkpoints.list", {"session_id": sid})
        assert msg["ok"] is True
        assert len(msg["payload"]["checkpoints"]) == 2

        from opencomputer.gateway.protocol_v2 import CheckpointsListResult

        CheckpointsListResult.model_validate(msg["payload"])


@pytest.mark.asyncio
async def test_checkpoints_list_missing_param_is_error(tmp_path: Path) -> None:
    async with _wire_server(tmp_path) as (url, _db):
        msg = await _rpc(url, "checkpoints.list", {})
        assert msg["ok"] is False
        assert "session_id" in (msg.get("error") or "")


@pytest.mark.asyncio
async def test_checkpoints_delete_rpc(tmp_path: Path) -> None:
    async with _wire_server(tmp_path) as (url, db):
        sid = "cp-e2e-del"
        db.create_session(session_id=sid, platform="cli", model="m")
        cp_id = _make_checkpoint(db, sid, "doomed")

        msg = await _rpc(url, "checkpoints.delete", {"checkpoint_id": cp_id})
        assert msg["ok"] is True
        assert msg["payload"]["found"] is True
        # Side effect verified on disk.
        assert db.get_prompt_checkpoint(cp_id) is None


@pytest.mark.asyncio
async def test_checkpoints_delete_unknown_is_found_false(tmp_path: Path) -> None:
    async with _wire_server(tmp_path) as (url, _db):
        msg = await _rpc(url, "checkpoints.delete", {"checkpoint_id": "ghost"})
        assert msg["ok"] is True
        assert msg["payload"]["found"] is False


@pytest.mark.asyncio
async def test_checkpoints_delete_missing_param_is_error(tmp_path: Path) -> None:
    async with _wire_server(tmp_path) as (url, _db):
        msg = await _rpc(url, "checkpoints.delete", {})
        assert msg["ok"] is False
        assert "checkpoint_id" in (msg.get("error") or "")


@pytest.mark.asyncio
async def test_hello_handshake_advertises_batch8(tmp_path: Path) -> None:
    async with _wire_server(tmp_path) as (url, _db):
        msg = await _rpc(url, "hello", {})
        methods = msg["payload"]["methods"]
        assert "checkpoints.list" in methods
        assert "checkpoints.delete" in methods

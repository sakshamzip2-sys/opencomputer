"""Wire RPCs for session lifecycle — ``session.resume`` + ``session.delete``.

TUI-parity Milestone 1, batch 1 (spec:
``docs/superpowers/specs/2026-05-17-tui-parity/TUI.md``; mapping:
``docs/refs/hermes-tui-protocol-vs-oc-wire.md``).

The M1 protocol-mapping spike found OC's wire served 11 RPC methods while a
Hermes-parity TUI needs ~57. ``session.resume`` and ``session.delete`` are
the two highest-value missing ones: a resume picker cannot exist without a
method that returns a session's transcript, and a picker that can't delete
stale rows is half a feature.

Coverage mirrors ``test_wire_memory_status_rpc.py``:

* **Protocol surface** — constants + typed schemas + ``METHOD_SCHEMAS`` +
  hello-handshake advertises the new methods.
* **Helper** — ``WireServer._collect_session_resume`` degrades gracefully
  (missing session → None, no db → None, never raises).
* **End-to-end RPC** — real WireServer, real WS client, real SessionDB.
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


class TestSessionLifecycleProtocol:
    """Pin the wire-protocol surface for the two new methods."""

    def test_method_constants(self) -> None:
        from opencomputer.gateway.protocol import (
            METHOD_SESSION_DELETE,
            METHOD_SESSION_RESUME,
        )

        assert METHOD_SESSION_RESUME == "session.resume"
        assert METHOD_SESSION_DELETE == "session.delete"

    def test_methods_in_protocol_all(self) -> None:
        from opencomputer.gateway import protocol

        assert "METHOD_SESSION_RESUME" in protocol.__all__
        assert "METHOD_SESSION_DELETE" in protocol.__all__

    def test_schemas_in_protocol_v2_all(self) -> None:
        from opencomputer.gateway import protocol_v2

        for name in (
            "METHOD_SESSION_RESUME",
            "METHOD_SESSION_DELETE",
            "SessionResumeParams",
            "SessionResumeResult",
            "SessionDeleteParams",
            "SessionDeleteResult",
            "TranscriptMessage",
        ):
            assert name in protocol_v2.__all__, f"missing __all__: {name}"

    def test_method_schemas_registry(self) -> None:
        from opencomputer.gateway.protocol import (
            METHOD_SESSION_DELETE,
            METHOD_SESSION_RESUME,
        )
        from opencomputer.gateway.protocol_v2 import (
            METHOD_SCHEMAS,
            SessionDeleteParams,
            SessionDeleteResult,
            SessionResumeParams,
            SessionResumeResult,
        )

        assert METHOD_SCHEMAS[METHOD_SESSION_RESUME] == (
            SessionResumeParams,
            SessionResumeResult,
        )
        assert METHOD_SCHEMAS[METHOD_SESSION_DELETE] == (
            SessionDeleteParams,
            SessionDeleteResult,
        )

    def test_resume_result_round_trip(self) -> None:
        from opencomputer.gateway.protocol_v2 import (
            SessionResumeResult,
            TranscriptMessage,
        )

        result = SessionResumeResult(
            session_id="abc",
            info={"id": "abc", "title": "hi"},
            messages=(
                TranscriptMessage(role="user", text="hello"),
                TranscriptMessage(role="assistant", text="hi there"),
            ),
            message_count=2,
        )
        restored = SessionResumeResult.model_validate_json(result.model_dump_json())
        assert restored == result
        assert len(restored.messages) == 2

    def test_delete_result_round_trip(self) -> None:
        from opencomputer.gateway.protocol_v2 import SessionDeleteResult

        result = SessionDeleteResult(deleted="abc", found=True)
        restored = SessionDeleteResult.model_validate_json(result.model_dump_json())
        assert restored == result

    def test_params_reject_unknown_field(self) -> None:
        from opencomputer.gateway.protocol_v2 import SessionResumeParams

        with pytest.raises(Exception):  # pydantic.ValidationError
            SessionResumeParams(session_id="x", bogus="surprise")


# ─── _collect_session_resume helper unit tests ─────────────────────


class TestCollectSessionResumeHelper:
    """The static helper never raises; missing data → None."""

    def test_loop_without_db_returns_none(self) -> None:
        from opencomputer.gateway.wire_server import WireServer

        loop = MagicMock(spec=[])  # no attrs at all
        assert WireServer._collect_session_resume(loop, "any") is None

    def test_unknown_session_returns_none(self, tmp_path: Path) -> None:
        from opencomputer.agent.state import SessionDB
        from opencomputer.gateway.wire_server import WireServer

        db = SessionDB(tmp_path / "sessions.db")
        loop = MagicMock()
        loop.db = db
        assert WireServer._collect_session_resume(loop, "nope") is None

    def test_known_session_returns_transcript(self, tmp_path: Path) -> None:
        from opencomputer.agent.state import SessionDB
        from opencomputer.gateway.wire_server import WireServer
        from plugin_sdk.core import Message

        db = SessionDB(tmp_path / "sessions.db")
        sid = "sess-resume-0001"
        db.create_session(session_id=sid, platform="cli", model="m", title="T")
        db.append_message(sid, Message(role="user", content="ping"))
        db.append_message(sid, Message(role="assistant", content="pong"))

        loop = MagicMock()
        loop.db = db
        out = WireServer._collect_session_resume(loop, sid)
        assert out is not None
        assert out["session_id"] == sid
        assert out["message_count"] == 2
        assert out["info"]["title"] == "T"
        assert [m["text"] for m in out["messages"]] == ["ping", "pong"]
        assert out["messages"][0]["role"] == "user"


# ─── End-to-end RPC over a real WS ─────────────────────────────────


@contextlib.asynccontextmanager
async def _wire_server(tmp_path: Path):
    """Spin up a real WireServer wired to a real SessionDB."""
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
        yield server, f"ws://127.0.0.1:{port}", db
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
async def test_session_resume_rpc_returns_transcript(tmp_path: Path) -> None:
    from plugin_sdk.core import Message

    async with _wire_server(tmp_path) as (_srv, url, db):
        sid = "sess-e2e-resume-1"
        db.create_session(session_id=sid, platform="cli", model="m", title="E2E")
        db.append_message(sid, Message(role="user", content="hello"))
        db.append_message(sid, Message(role="assistant", content="world"))

        msg = await _rpc(url, "session.resume", {"session_id": sid})
        assert msg["type"] == "res"
        assert msg["ok"] is True
        payload = msg["payload"]
        assert payload["session_id"] == sid
        assert payload["message_count"] == 2
        assert payload["messages"][1]["text"] == "world"

        from opencomputer.gateway.protocol_v2 import SessionResumeResult

        SessionResumeResult.model_validate(payload)


@pytest.mark.asyncio
async def test_session_resume_unknown_id_is_error(tmp_path: Path) -> None:
    async with _wire_server(tmp_path) as (_srv, url, _db):
        msg = await _rpc(url, "session.resume", {"session_id": "ghost"})
        assert msg["ok"] is False
        assert "session.resume" in (msg.get("error") or "")


@pytest.mark.asyncio
async def test_session_resume_missing_param_is_error(tmp_path: Path) -> None:
    async with _wire_server(tmp_path) as (_srv, url, _db):
        msg = await _rpc(url, "session.resume", {})
        assert msg["ok"] is False
        assert "session_id" in (msg.get("error") or "")


@pytest.mark.asyncio
async def test_session_delete_rpc_removes_session(tmp_path: Path) -> None:
    async with _wire_server(tmp_path) as (_srv, url, db):
        sid = "sess-e2e-delete-1"
        db.create_session(session_id=sid, platform="cli", model="m")
        assert db.get_session(sid) is not None

        msg = await _rpc(url, "session.delete", {"session_id": sid})
        assert msg["ok"] is True
        assert msg["payload"]["deleted"] == sid
        assert msg["payload"]["found"] is True
        # Side effect verified on disk, not just the response.
        assert db.get_session(sid) is None


@pytest.mark.asyncio
async def test_session_delete_unknown_id_reports_not_found(tmp_path: Path) -> None:
    async with _wire_server(tmp_path) as (_srv, url, _db):
        msg = await _rpc(url, "session.delete", {"session_id": "ghost"})
        # Deleting a non-existent id is not an error — it's idempotent —
        # but found=False tells the client nothing was removed.
        assert msg["ok"] is True
        assert msg["payload"]["found"] is False


@pytest.mark.asyncio
async def test_session_delete_missing_param_is_error(tmp_path: Path) -> None:
    async with _wire_server(tmp_path) as (_srv, url, _db):
        msg = await _rpc(url, "session.delete", {})
        assert msg["ok"] is False
        assert "session_id" in (msg.get("error") or "")


@pytest.mark.asyncio
async def test_hello_handshake_advertises_session_lifecycle(tmp_path: Path) -> None:
    async with _wire_server(tmp_path) as (_srv, url, _db):
        msg = await _rpc(url, "hello", {})
        assert msg["ok"] is True
        methods = msg["payload"]["methods"]
        assert "session.resume" in methods
        assert "session.delete" in methods

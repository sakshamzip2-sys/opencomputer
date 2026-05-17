"""Wire RPCs ``skill.show`` + ``session.fork`` — TUI-parity M1 batch 6.

Spec: ``docs/superpowers/specs/2026-05-17-tui-parity/TUI.md``.

* ``skill.show`` — a skill's full SKILL.md body. Powers the skills-hub
  overlay's "preview before invoking" affordance.
* ``session.fork`` — clone a session's history into a new session id.
  Powers the fork-tree / branch affordance.

Coverage mirrors ``test_wire_session_lifecycle.py``: protocol surface +
graceful helper units + end-to-end RPC over a real WS.
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


class TestSkillForkProtocol:
    def test_method_constants(self) -> None:
        from opencomputer.gateway.protocol import (
            METHOD_SESSION_FORK,
            METHOD_SKILL_SHOW,
        )

        assert METHOD_SKILL_SHOW == "skill.show"
        assert METHOD_SESSION_FORK == "session.fork"

    def test_methods_in_protocol_all(self) -> None:
        from opencomputer.gateway import protocol

        assert "METHOD_SKILL_SHOW" in protocol.__all__
        assert "METHOD_SESSION_FORK" in protocol.__all__

    def test_schemas_in_protocol_v2_all(self) -> None:
        from opencomputer.gateway import protocol_v2

        for name in (
            "METHOD_SKILL_SHOW",
            "METHOD_SESSION_FORK",
            "SkillShowParams",
            "SkillShowResult",
            "SessionForkParams",
            "SessionForkResult",
        ):
            assert name in protocol_v2.__all__, f"missing __all__: {name}"

    def test_method_schemas_registry(self) -> None:
        from opencomputer.gateway.protocol import (
            METHOD_SESSION_FORK,
            METHOD_SKILL_SHOW,
        )
        from opencomputer.gateway.protocol_v2 import (
            METHOD_SCHEMAS,
            SessionForkParams,
            SessionForkResult,
            SkillShowParams,
            SkillShowResult,
        )

        assert METHOD_SCHEMAS[METHOD_SKILL_SHOW] == (
            SkillShowParams,
            SkillShowResult,
        )
        assert METHOD_SCHEMAS[METHOD_SESSION_FORK] == (
            SessionForkParams,
            SessionForkResult,
        )

    def test_result_round_trips(self) -> None:
        from opencomputer.gateway.protocol_v2 import (
            SessionForkResult,
            SkillShowResult,
        )

        s = SkillShowResult(skill_id="x", body="# Body", found=True)
        assert SkillShowResult.model_validate_json(s.model_dump_json()) == s
        f = SessionForkResult(
            source_session_id="src",
            new_session_id="new",
            messages_copied=3,
            ok=True,
        )
        assert SessionForkResult.model_validate_json(f.model_dump_json()) == f

    def test_params_reject_unknown_field(self) -> None:
        from opencomputer.gateway.protocol_v2 import SkillShowParams

        with pytest.raises(Exception):  # pydantic.ValidationError
            SkillShowParams(skill_id="x", bogus="surprise")


# ─── helper unit tests ─────────────────────────────────────────────


def _memory(tmp_path: Path):
    from opencomputer.agent.memory import MemoryManager

    return MemoryManager(
        declarative_path=tmp_path / "MEMORY.md",
        user_path=tmp_path / "USER.md",
        skills_path=tmp_path / "skills",
    )


class TestCollectSkillBodyHelper:
    def test_loop_without_memory_returns_not_found(self) -> None:
        from opencomputer.gateway.wire_server import WireServer

        loop = MagicMock(spec=[])
        out = WireServer._collect_skill_body(loop, "any")
        assert out["found"] is False
        assert out["body"] == ""

    def test_unknown_skill_returns_not_found(self, tmp_path: Path) -> None:
        from opencomputer.gateway.wire_server import WireServer

        loop = MagicMock()
        loop.memory = _memory(tmp_path)
        out = WireServer._collect_skill_body(loop, "ghost-skill")
        assert out["found"] is False

    def test_known_skill_returns_body(self, tmp_path: Path) -> None:
        from opencomputer.gateway.wire_server import WireServer

        mm = _memory(tmp_path)
        mm.write_skill("my-skill", "a test skill", "## How it works\nDo the thing.")
        loop = MagicMock()
        loop.memory = mm
        out = WireServer._collect_skill_body(loop, "my-skill")
        assert out["found"] is True
        assert "Do the thing." in out["body"]
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
    mm = _memory(tmp_path)
    fake_loop = MagicMock(spec=AgentLoop)
    fake_loop.db = db
    fake_loop.memory = mm
    server = WireServer(loop=fake_loop, host="127.0.0.1", port=port)
    await server.start()
    try:
        yield f"ws://127.0.0.1:{port}", db, mm
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
async def test_skill_show_rpc_returns_body(tmp_path: Path) -> None:
    async with _wire_server(tmp_path) as (url, _db, mm):
        mm.write_skill("e2e-skill", "desc", "# E2E\nThe skill body.")
        msg = await _rpc(url, "skill.show", {"skill_id": "e2e-skill"})
        assert msg["ok"] is True
        assert msg["payload"]["found"] is True
        assert "The skill body." in msg["payload"]["body"]

        from opencomputer.gateway.protocol_v2 import SkillShowResult

        SkillShowResult.model_validate(msg["payload"])


@pytest.mark.asyncio
async def test_skill_show_rpc_unknown_is_found_false(tmp_path: Path) -> None:
    async with _wire_server(tmp_path) as (url, _db, _mm):
        msg = await _rpc(url, "skill.show", {"skill_id": "nope"})
        assert msg["ok"] is True
        assert msg["payload"]["found"] is False


@pytest.mark.asyncio
async def test_skill_show_rpc_missing_param_is_error(tmp_path: Path) -> None:
    async with _wire_server(tmp_path) as (url, _db, _mm):
        msg = await _rpc(url, "skill.show", {})
        assert msg["ok"] is False
        assert "skill_id" in (msg.get("error") or "")


@pytest.mark.asyncio
async def test_session_fork_rpc_clones_history(tmp_path: Path) -> None:
    from plugin_sdk.core import Message

    async with _wire_server(tmp_path) as (url, db, _mm):
        sid = "fork-source-1"
        db.create_session(session_id=sid, platform="cli", model="m", title="Src")
        db.append_message(sid, Message(role="user", content="hello"))
        db.append_message(sid, Message(role="assistant", content="hi"))

        msg = await _rpc(url, "session.fork", {"session_id": sid})
        assert msg["ok"] is True
        payload = msg["payload"]
        assert payload["ok"] is True
        assert payload["source_session_id"] == sid
        assert payload["messages_copied"] == 2
        new_id = payload["new_session_id"]
        assert new_id and new_id != sid

        from opencomputer.gateway.protocol_v2 import SessionForkResult

        SessionForkResult.model_validate(payload)
        # The new session genuinely exists with the copied transcript.
        assert db.get_session(new_id) is not None
        assert len(db.get_messages(new_id)) == 2


@pytest.mark.asyncio
async def test_session_fork_rpc_unknown_source_is_error(tmp_path: Path) -> None:
    async with _wire_server(tmp_path) as (url, _db, _mm):
        msg = await _rpc(url, "session.fork", {"session_id": "ghost"})
        assert msg["ok"] is False
        assert "session.fork" in (msg.get("error") or "")


@pytest.mark.asyncio
async def test_session_fork_rpc_missing_param_is_error(tmp_path: Path) -> None:
    async with _wire_server(tmp_path) as (url, _db, _mm):
        msg = await _rpc(url, "session.fork", {})
        assert msg["ok"] is False
        assert "session_id" in (msg.get("error") or "")


@pytest.mark.asyncio
async def test_hello_handshake_advertises_batch6(tmp_path: Path) -> None:
    async with _wire_server(tmp_path) as (url, _db, _mm):
        msg = await _rpc(url, "hello", {})
        methods = msg["payload"]["methods"]
        assert "skill.show" in methods
        assert "session.fork" in methods

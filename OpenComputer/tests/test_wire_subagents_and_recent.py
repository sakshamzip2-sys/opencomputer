"""Wire RPCs ``subagents.list`` + ``session.most_recent`` — TUI-parity M1 batch 5.

Spec: ``docs/superpowers/specs/2026-05-17-tui-parity/TUI.md``.

* ``subagents.list`` — spawned-subagent history (wraps SubagentStore).
  Powers an agents overlay listing running/completed delegated agents.
* ``session.most_recent`` — the latest session's id+title+timestamp.
  Powers the TUI's "resume last session" affordance.

Coverage mirrors ``test_wire_session_lifecycle.py``: protocol surface +
graceful helper units + end-to-end RPC over a real WS.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
from datetime import UTC, datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ─── Protocol surface ──────────────────────────────────────────────


class TestSubagentsRecentProtocol:
    def test_method_constants(self) -> None:
        from opencomputer.gateway.protocol import (
            METHOD_SESSION_MOST_RECENT,
            METHOD_SUBAGENTS_LIST,
        )

        assert METHOD_SUBAGENTS_LIST == "subagents.list"
        assert METHOD_SESSION_MOST_RECENT == "session.most_recent"

    def test_methods_in_protocol_all(self) -> None:
        from opencomputer.gateway import protocol

        assert "METHOD_SUBAGENTS_LIST" in protocol.__all__
        assert "METHOD_SESSION_MOST_RECENT" in protocol.__all__

    def test_schemas_in_protocol_v2_all(self) -> None:
        from opencomputer.gateway import protocol_v2

        for name in (
            "METHOD_SUBAGENTS_LIST",
            "METHOD_SESSION_MOST_RECENT",
            "SubagentsListParams",
            "SubagentsListResult",
            "SubagentInfo",
            "SessionMostRecentParams",
            "SessionMostRecentResult",
        ):
            assert name in protocol_v2.__all__, f"missing __all__: {name}"

    def test_method_schemas_registry(self) -> None:
        from opencomputer.gateway.protocol import (
            METHOD_SESSION_MOST_RECENT,
            METHOD_SUBAGENTS_LIST,
        )
        from opencomputer.gateway.protocol_v2 import (
            METHOD_SCHEMAS,
            SessionMostRecentParams,
            SessionMostRecentResult,
            SubagentsListParams,
            SubagentsListResult,
        )

        assert METHOD_SCHEMAS[METHOD_SUBAGENTS_LIST] == (
            SubagentsListParams,
            SubagentsListResult,
        )
        assert METHOD_SCHEMAS[METHOD_SESSION_MOST_RECENT] == (
            SessionMostRecentParams,
            SessionMostRecentResult,
        )

    def test_result_round_trips(self) -> None:
        from opencomputer.gateway.protocol_v2 import (
            SessionMostRecentResult,
            SubagentInfo,
            SubagentsListResult,
        )

        s = SubagentsListResult(
            subagents=(
                SubagentInfo(
                    agent_id="a1",
                    goal="do a thing",
                    state="completed",
                    display_state="completed",
                    role="leaf",
                    depth=0,
                    started_at="2026-05-17T00:00:00+00:00",
                ),
            )
        )
        assert SubagentsListResult.model_validate_json(s.model_dump_json()) == s
        r = SessionMostRecentResult(found=True, session_id="s", title="T")
        assert SessionMostRecentResult.model_validate_json(r.model_dump_json()) == r


# ─── helper unit tests ─────────────────────────────────────────────


class TestCollectSubagentsHelper:
    def test_loop_without_db_returns_empty(self) -> None:
        from opencomputer.gateway.wire_server import WireServer

        loop = MagicMock(spec=[])
        assert WireServer._collect_subagents(loop, limit=50, running_only=False) == []

    def test_empty_store_returns_empty(self, tmp_path: Path) -> None:
        from opencomputer.agent.state import SessionDB
        from opencomputer.gateway.wire_server import WireServer

        db = SessionDB(tmp_path / "sessions.db")
        loop = MagicMock()
        loop.db = db
        out = WireServer._collect_subagents(loop, limit=50, running_only=False)
        assert out == []

    def test_populated_store_returns_records(self, tmp_path: Path) -> None:
        from opencomputer.agent.state import SessionDB
        from opencomputer.agent.subagent_store import SubagentStore
        from opencomputer.gateway.wire_server import WireServer

        db = SessionDB(tmp_path / "sessions.db")
        store = SubagentStore(db.db_path, allow_create=True)
        store.upsert(
            agent_id="agent-x",
            parent_session_id="parent-s",
            child_session_id="child-s",
            parent_agent_id=None,
            goal="investigate the bug",
            started_at=datetime.now(UTC),
            state="completed",
            role="leaf",
        )
        loop = MagicMock()
        loop.db = db
        out = WireServer._collect_subagents(loop, limit=50, running_only=False)
        assert len(out) == 1
        assert out[0]["agent_id"] == "agent-x"
        assert out[0]["goal"] == "investigate the bug"
        assert out[0]["state"] == "completed"
        json.dumps(out)  # fully JSON-safe (datetimes → iso strings)


class TestCollectMostRecentHelper:
    def test_loop_without_db(self) -> None:
        from opencomputer.gateway.wire_server import WireServer

        loop = MagicMock(spec=[])
        out = WireServer._collect_most_recent(loop)
        assert out["found"] is False

    def test_empty_db_found_false(self, tmp_path: Path) -> None:
        from opencomputer.agent.state import SessionDB
        from opencomputer.gateway.wire_server import WireServer

        db = SessionDB(tmp_path / "sessions.db")
        loop = MagicMock()
        loop.db = db
        assert WireServer._collect_most_recent(loop)["found"] is False

    def test_returns_latest_session(self, tmp_path: Path) -> None:
        from opencomputer.agent.state import SessionDB
        from opencomputer.gateway.wire_server import WireServer

        db = SessionDB(tmp_path / "sessions.db")
        db.create_session(session_id="older", platform="cli", model="m")
        db.create_session(session_id="newer", platform="cli", model="m")
        loop = MagicMock()
        loop.db = db
        out = WireServer._collect_most_recent(loop)
        assert out["found"] is True
        assert out["session_id"] in {"older", "newer"}


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
async def test_subagents_list_rpc_empty(tmp_path: Path) -> None:
    async with _wire_server(tmp_path) as (url, _db):
        msg = await _rpc(url, "subagents.list", {})
        assert msg["ok"] is True
        assert msg["payload"]["subagents"] == []

        from opencomputer.gateway.protocol_v2 import SubagentsListResult

        SubagentsListResult.model_validate(msg["payload"])


@pytest.mark.asyncio
async def test_subagents_list_rpc_populated(tmp_path: Path) -> None:
    from opencomputer.agent.subagent_store import SubagentStore

    async with _wire_server(tmp_path) as (url, db):
        store = SubagentStore(db.db_path, allow_create=True)
        store.upsert(
            agent_id="e2e-agent",
            parent_session_id="ps",
            child_session_id="cs",
            parent_agent_id=None,
            goal="run the e2e check",
            started_at=datetime.now(UTC),
            state="running",
            role="leaf",
        )
        msg = await _rpc(url, "subagents.list", {})
        assert msg["ok"] is True
        subs = msg["payload"]["subagents"]
        assert len(subs) == 1
        assert subs[0]["agent_id"] == "e2e-agent"

        from opencomputer.gateway.protocol_v2 import SubagentsListResult

        SubagentsListResult.model_validate(msg["payload"])


@pytest.mark.asyncio
async def test_session_most_recent_rpc(tmp_path: Path) -> None:
    async with _wire_server(tmp_path) as (url, db):
        db.create_session(session_id="recent-1", platform="cli", model="m")
        msg = await _rpc(url, "session.most_recent", {})
        assert msg["ok"] is True
        assert msg["payload"]["found"] is True

        from opencomputer.gateway.protocol_v2 import SessionMostRecentResult

        SessionMostRecentResult.model_validate(msg["payload"])


@pytest.mark.asyncio
async def test_session_most_recent_rpc_empty_db(tmp_path: Path) -> None:
    async with _wire_server(tmp_path) as (url, _db):
        msg = await _rpc(url, "session.most_recent", {})
        assert msg["ok"] is True
        assert msg["payload"]["found"] is False


@pytest.mark.asyncio
async def test_hello_handshake_advertises_batch5(tmp_path: Path) -> None:
    async with _wire_server(tmp_path) as (url, _db):
        msg = await _rpc(url, "hello", {})
        methods = msg["payload"]["methods"]
        assert "subagents.list" in methods
        assert "session.most_recent" in methods

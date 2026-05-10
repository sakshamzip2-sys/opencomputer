"""permissions_respond — 10th MCP tool (F1 consent grant/revoke write-back)."""

from __future__ import annotations

import sqlite3
import time

import pytest

from opencomputer.agent.consent.store import ConsentStore
from opencomputer.agent.state import SessionDB
from opencomputer.mcp.server import build_server


@pytest.fixture
def isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    # Initialize sessions.db so ConsentStore has tables to write to.
    SessionDB(tmp_path / "sessions.db")
    return tmp_path


def _call(server, name: str, **args):
    """Invoke a FastMCP tool by name and return the dict result."""
    handler = None
    for tool_meta in server._tool_manager._tools.values():
        if tool_meta.name == name:
            handler = tool_meta.fn
            break
    assert handler is not None, f"tool {name!r} not registered"
    return handler(**args)


def test_permissions_respond_grants_capability(isolated_home):
    server = build_server()
    result = _call(
        server,
        "permissions_respond",
        capability_id="fs.read",
        decision="allow",
        scope="/tmp",
        tier=1,
    )
    assert result["ok"] is True
    assert result["action"] == "granted"
    assert result["capability_id"] == "fs.read"

    with sqlite3.connect(str(isolated_home / "sessions.db")) as conn:
        store = ConsentStore(conn)
        grant = store.get("fs.read", "/tmp")
        assert grant is not None
        assert int(grant.tier) == 1


def test_permissions_respond_revokes_capability(isolated_home):
    server = build_server()
    _call(server, "permissions_respond", capability_id="shell.exec",
          decision="allow", scope=None, tier=2)
    revoke = _call(server, "permissions_respond", capability_id="shell.exec",
                   decision="deny", scope=None)
    assert revoke["ok"] is True
    assert revoke["action"] == "revoked"

    with sqlite3.connect(str(isolated_home / "sessions.db")) as conn:
        store = ConsentStore(conn)
        assert store.get("shell.exec", None) is None


def test_permissions_respond_with_expiry(isolated_home):
    server = build_server()
    result = _call(
        server,
        "permissions_respond",
        capability_id="net.fetch",
        decision="allow",
        tier=3,
        expires_in_seconds=3600,
    )
    assert result["ok"] is True
    with sqlite3.connect(str(isolated_home / "sessions.db")) as conn:
        store = ConsentStore(conn)
        grant = store.get("net.fetch", None)
        assert grant is not None
        assert grant.expires_at is not None
        # within ~5s of now+3600
        assert abs(grant.expires_at - (time.time() + 3600)) < 5


def test_permissions_respond_rejects_bad_decision(isolated_home):
    server = build_server()
    result = _call(
        server,
        "permissions_respond",
        capability_id="x",
        decision="maybe",
    )
    assert result["ok"] is False
    assert "decision" in result["error"].lower()


def test_permissions_respond_rejects_bad_tier(isolated_home):
    server = build_server()
    result = _call(
        server,
        "permissions_respond",
        capability_id="x",
        decision="allow",
        tier=99,
    )
    assert result["ok"] is False
    assert "tier" in result["error"].lower()

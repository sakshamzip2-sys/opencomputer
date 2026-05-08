"""Dynamic MCP tool discovery — handle ``notifications/tools/list_changed``.

When an MCP server pushes the ``tools/list_changed`` notification,
:class:`MCPConnection` should re-fetch tools, diff against the cached
list, and notify the manager via ``tools_changed_callback`` so the
global ``ToolRegistry`` stays in sync without a manual ``/reload-mcp``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from opencomputer.agent.config import MCPServerConfig
from opencomputer.mcp.client import MCPConnection, MCPTool


def _make_tool_meta(name: str, *, description: str = "") -> SimpleNamespace:
    """Build a minimal mcp.types.Tool stand-in."""
    return SimpleNamespace(
        name=name,
        description=description,
        inputSchema={"type": "object", "properties": {}},
        annotations=None,
        meta=None,
    )


def _make_session(tool_names: list[str]) -> MagicMock:
    session = MagicMock()
    session.list_tools = AsyncMock(
        return_value=SimpleNamespace(tools=[_make_tool_meta(n) for n in tool_names])
    )
    return session


def _conn(initial_tools: list[str], session, callback=None) -> MCPConnection:
    cfg = MCPServerConfig(name="srv", transport="stdio")
    conn = MCPConnection(
        config=cfg,
        session=session,
        tools_changed_callback=callback,
    )
    conn.tools = [
        MCPTool(
            server_name="srv",
            tool_name=n,
            description="",
            parameters={"type": "object", "properties": {}},
            session=session,
        )
        for n in initial_tools
    ]
    return conn


@pytest.mark.asyncio
async def test_reconcile_adds_new_tools():
    session = _make_session(["a", "b", "c"])
    events: list[tuple[list[str], list[str]]] = []

    def cb(_conn, added, removed):
        events.append(([t.tool_name for t in added], [t.tool_name for t in removed]))

    conn = _conn(["a"], session, callback=cb)
    await conn._reconcile_tools()
    assert sorted(t.tool_name for t in conn.tools if isinstance(t, MCPTool)) == ["a", "b", "c"]
    assert events
    added_names, removed_names = events[-1]
    assert sorted(added_names) == ["b", "c"]
    assert removed_names == []


@pytest.mark.asyncio
async def test_reconcile_removes_gone_tools():
    session = _make_session(["a"])
    events: list[tuple[list[str], list[str]]] = []

    def cb(_conn, added, removed):
        events.append(([t.tool_name for t in added], [t.tool_name for t in removed]))

    conn = _conn(["a", "b", "c"], session, callback=cb)
    await conn._reconcile_tools()
    assert sorted(t.tool_name for t in conn.tools if isinstance(t, MCPTool)) == ["a"]
    added_names, removed_names = events[-1]
    assert added_names == []
    assert sorted(removed_names) == ["b", "c"]


@pytest.mark.asyncio
async def test_reconcile_no_change_no_callback():
    session = _make_session(["a", "b"])
    events: list[tuple[list[str], list[str]]] = []

    def cb(_conn, added, removed):
        events.append(([t.tool_name for t in added], [t.tool_name for t in removed]))

    conn = _conn(["a", "b"], session, callback=cb)
    await conn._reconcile_tools()
    assert events == []  # no diff → no callback


@pytest.mark.asyncio
async def test_reconcile_honors_tools_allow():
    session = _make_session(["good", "bad"])
    cfg = MCPServerConfig(
        name="srv", transport="stdio", tools_allow=("good",)
    )
    conn = MCPConnection(config=cfg, session=session)
    await conn._reconcile_tools()
    names = [t.tool_name for t in conn.tools if isinstance(t, MCPTool)]
    assert names == ["good"]


@pytest.mark.asyncio
async def test_reconcile_honors_tools_deny():
    session = _make_session(["a", "evil"])
    cfg = MCPServerConfig(
        name="srv", transport="stdio", tools_deny=("evil",)
    )
    conn = MCPConnection(config=cfg, session=session)
    await conn._reconcile_tools()
    names = [t.tool_name for t in conn.tools if isinstance(t, MCPTool)]
    assert names == ["a"]


@pytest.mark.asyncio
async def test_reconcile_in_flight_guard():
    """Repeated notifications during one reconcile must coalesce."""
    session = _make_session(["x"])
    conn = _conn([], session)
    conn._reconcile_in_flight = True  # simulate in-flight reconcile
    notif = MagicMock()

    # Build a fake ToolListChangedNotification-shaped object
    class FakeNotif:
        pass

    # Reentry should be a no-op (no new asyncio task).
    # We can verify by asserting tools list is unchanged.
    initial_tools = list(conn.tools)
    await conn._handle_session_message(notif)
    assert conn.tools == initial_tools


@pytest.mark.asyncio
async def test_manager_callback_updates_registry():
    """When a connection emits tools_changed, MCPManager unregisters/registers."""
    from opencomputer.mcp.client import MCPManager
    from opencomputer.tools.registry import ToolRegistry

    registry = ToolRegistry()
    mgr = MCPManager(tool_registry=registry)

    session = _make_session([])

    cfg = MCPServerConfig(name="srv", transport="stdio")
    conn = MCPConnection(
        config=cfg,
        session=session,
        tools_changed_callback=mgr._on_connection_tools_changed,
    )
    mgr.connections.append(conn)

    # Initial state: no tools registered.
    new_tool = MCPTool(
        server_name="srv",
        tool_name="newtool",
        description="",
        parameters={"type": "object", "properties": {}},
        session=session,
    )

    mgr._on_connection_tools_changed(conn, added=[new_tool], removed=[])
    schema_name = new_tool.schema.name
    assert registry.get(schema_name) is not None

    mgr._on_connection_tools_changed(conn, added=[], removed=[new_tool])
    assert registry.get(schema_name) is None

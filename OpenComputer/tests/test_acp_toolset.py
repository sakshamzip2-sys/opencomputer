"""T62 — ACP toolset registration via tools/list + session/toolset notification."""

from __future__ import annotations

import pytest

from opencomputer.acp.server import ACPServer


class _FakeSchema:
    def __init__(self, name: str, description: str, params: dict) -> None:
        self.name = name
        self.description = description
        self.parameters = params


class _FakeTool:
    def __init__(self, name: str, description: str, params: dict) -> None:
        self.schema = _FakeSchema(name, description, params)


@pytest.fixture
def fake_registry(monkeypatch):
    """Stub the singleton registry with two predictable tools."""
    from opencomputer.tools import registry as registry_mod

    fake = registry_mod.ToolRegistry()
    fake.register(
        _FakeTool(
            "Echo",
            "Echo input back",
            {"type": "object", "properties": {"text": {"type": "string"}}},
        )
    )
    fake.register(
        _FakeTool(
            "AddNumbers",
            "Add two numbers",
            {
                "type": "object",
                "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
            },
        )
    )
    monkeypatch.setattr(registry_mod, "registry", fake)
    return fake


@pytest.fixture
def server_with_capture():
    server = ACPServer()
    captured: list[dict] = []
    server._write = lambda msg: captured.append(msg)
    return server, captured


@pytest.mark.asyncio
async def test_tools_list_returns_registered_tools(server_with_capture, fake_registry):
    server, captured = server_with_capture
    await server._dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    )
    await server._dispatch(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    )
    result = captured[1]["result"]
    tools = result["tools"]
    assert len(tools) == 2
    by_name = {t["name"]: t for t in tools}
    assert "Echo" in by_name and "AddNumbers" in by_name
    echo = by_name["Echo"]
    assert echo["description"] == "Echo input back"
    assert echo["input_schema"]["type"] == "object"


@pytest.mark.asyncio
async def test_tools_list_works_before_session_created(server_with_capture, fake_registry):
    """tools/list is session-independent — should work right after initialize."""
    server, captured = server_with_capture
    await server._dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    )
    await server._dispatch(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
    )
    assert "result" in captured[1]
    assert isinstance(captured[1]["result"]["tools"], list)


@pytest.mark.asyncio
async def test_tools_list_requires_initialize(server_with_capture, fake_registry):
    """Like every other method: tools/list rejected before initialize."""
    server, captured = server_with_capture
    await server._dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    )
    assert "error" in captured[0]
    assert "not initialized" in captured[0]["error"]["message"]


@pytest.mark.asyncio
async def test_new_session_emits_session_toolset_notification(
    server_with_capture, fake_registry
):
    """Proactive announcement so IDEs don't have to poll."""
    import asyncio

    server, captured = server_with_capture
    await server._dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    )
    await server._dispatch(
        {"jsonrpc": "2.0", "id": 2, "method": "newSession", "params": {}}
    )
    # Yield so the scheduled toolset announcement task runs.
    await asyncio.sleep(0)
    notifications = [m for m in captured if "method" in m and "id" not in m]
    toolset_notes = [n for n in notifications if n["method"] == "session/toolset"]
    assert len(toolset_notes) == 1
    note = toolset_notes[0]
    assert "sessionId" in note["params"]
    assert isinstance(note["params"]["tools"], list)
    assert len(note["params"]["tools"]) == 2


@pytest.mark.asyncio
async def test_initialize_advertises_toolset_capability(server_with_capture, fake_registry):
    server, captured = server_with_capture
    await server._dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    )
    caps = captured[0]["result"]["serverCapabilities"]
    assert caps["toolset"] is True

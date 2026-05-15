"""Gap G — first-tool-call lazy wakeup for bundle MCP servers.

mcp-openclaw-port follow-up. With lazy=True, a bundle MCP doesn't
auto-mount at chat start — users had to manually ``oc mcp enable``.
This module adds the OpenClaw / Hermes pattern:

1. Plugin manifest's ``bundle_mcp[i].tools`` declares the tools the
   server exposes (name + description + input_schema).
2. At plugin activation, the loader registers ``LazyBundleStubTool``
   instances by those names in the tool registry. Stubs satisfy
   tool-listing immediately — the LLM sees the tool available.
3. First dispatch through a stub: connect the bundle MCP, look up
   the real tool, and route the call to it. Subsequent calls reuse
   the cached real tool.

Covers:
- BundleMcpToolDecl dataclass shape + manifest roundtrip
- LazyBundleStubTool wakeup + dispatch + caching
- Wakeup failure returns a clean error ToolResult
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from opencomputer.mcp.lazy_wakeup import (
    BundleWakeupError,
    LazyBundleStubTool,
)
from opencomputer.plugins.discovery import _parse_manifest
from plugin_sdk.core import BundleMcpServer, BundleMcpToolDecl, ToolCall


# ─── BundleMcpToolDecl dataclass shape ───────────────────────────


def test_bundle_mcp_tool_decl_frozen() -> None:
    decl = BundleMcpToolDecl(name="read_file", description="Read a file")
    with pytest.raises(Exception):
        decl.name = "evil"  # type: ignore[misc]


def test_bundle_mcp_tool_decl_defaults() -> None:
    decl = BundleMcpToolDecl(name="x")
    assert decl.description == ""
    assert decl.input_schema == {}


def test_bundle_mcp_server_tools_default_empty() -> None:
    srv = BundleMcpServer(name="memory")
    assert srv.tools == ()


def test_bundle_mcp_server_with_tools() -> None:
    srv = BundleMcpServer(
        name="memory",
        tools=(
            BundleMcpToolDecl(name="store"),
            BundleMcpToolDecl(name="recall"),
        ),
    )
    assert len(srv.tools) == 2
    assert srv.tools[0].name == "store"


# ─── manifest roundtrip ──────────────────────────────────────────


def test_manifest_parses_tools_under_bundle_mcp(tmp_path: Path) -> None:
    plug_dir = tmp_path / "p"
    plug_dir.mkdir()
    manifest_data = {
        "id": "p",
        "name": "P",
        "version": "1.0.0",
        "entry": "plugin",
        "bundle_mcp": [
            {
                "name": "memory",
                "command": "npx",
                "lazy": True,
                "tools": [
                    {
                        "name": "store",
                        "description": "Store a memory",
                        "input_schema": {
                            "type": "object",
                            "properties": {"key": {"type": "string"}},
                        },
                    },
                ],
            },
        ],
    }
    (plug_dir / "plugin.json").write_text(json.dumps(manifest_data))
    manifest = _parse_manifest(plug_dir / "plugin.json")
    assert manifest is not None
    assert len(manifest.bundle_mcp) == 1
    bm = manifest.bundle_mcp[0]
    assert len(bm.tools) == 1
    assert bm.tools[0].name == "store"
    assert bm.tools[0].description == "Store a memory"
    assert bm.tools[0].input_schema["type"] == "object"


def test_manifest_without_tools_still_loads(tmp_path: Path) -> None:
    """Backwards-compat: bundle_mcp entries without ``tools`` still parse."""
    plug_dir = tmp_path / "p"
    plug_dir.mkdir()
    manifest_data = {
        "id": "p",
        "name": "P",
        "version": "1.0.0",
        "entry": "plugin",
        "bundle_mcp": [
            {"name": "memory", "command": "npx"},
        ],
    }
    (plug_dir / "plugin.json").write_text(json.dumps(manifest_data))
    manifest = _parse_manifest(plug_dir / "plugin.json")
    assert manifest is not None
    assert manifest.bundle_mcp[0].tools == ()


# ─── LazyBundleStubTool wakeup + dispatch ───────────────────────


def test_stub_tool_schema_exposes_declared_shape() -> None:
    decl = BundleMcpToolDecl(
        name="read_file",
        description="Read a file",
        input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
    )
    stub = LazyBundleStubTool(
        plugin_id="plug",
        server_name="memory",
        decl=decl,
        wakeup_fn=lambda: None,
        registry_lookup=lambda name: None,
    )
    schema = stub.schema
    assert schema.name == "plug__memory__read_file"
    assert schema.description == "Read a file"
    assert schema.parameters == {
        "type": "object", "properties": {"path": {"type": "string"}},
    }


def test_stub_tool_first_call_triggers_wakeup() -> None:
    wakeup_called: list[bool] = []
    real_tool = MagicMock()

    async def _real_execute(call: ToolCall):
        from plugin_sdk.core import ToolResult
        return ToolResult(tool_call_id=call.id, content="real result", is_error=False)

    real_tool.execute = _real_execute

    def wakeup():
        wakeup_called.append(True)

    def lookup(name: str):
        if name == "plug__memory__read_file" and wakeup_called:
            return real_tool
        return None

    stub = LazyBundleStubTool(
        plugin_id="plug",
        server_name="memory",
        decl=BundleMcpToolDecl(name="read_file"),
        wakeup_fn=wakeup,
        registry_lookup=lookup,
    )
    import asyncio
    result = asyncio.run(stub.execute(ToolCall(
        id="c1", name="plug__memory__read_file", arguments={},
    )))
    assert wakeup_called == [True]
    assert "real result" in result.content


def test_stub_tool_second_call_reuses_real_tool() -> None:
    """After first wakeup, subsequent calls don't re-trigger wakeup."""
    wakeup_called: list[bool] = []
    real_tool = MagicMock()

    async def _real_execute(call: ToolCall):
        from plugin_sdk.core import ToolResult
        return ToolResult(tool_call_id=call.id, content="ok", is_error=False)

    real_tool.execute = _real_execute

    def wakeup():
        wakeup_called.append(True)

    def lookup(name: str):
        return real_tool if wakeup_called else None

    stub = LazyBundleStubTool(
        plugin_id="plug",
        server_name="memory",
        decl=BundleMcpToolDecl(name="read_file"),
        wakeup_fn=wakeup,
        registry_lookup=lookup,
    )
    import asyncio
    asyncio.run(stub.execute(ToolCall(id="c1", name="x", arguments={})))
    asyncio.run(stub.execute(ToolCall(id="c2", name="x", arguments={})))
    # Wakeup ran ONCE
    assert len(wakeup_called) == 1


def test_stub_tool_wakeup_failure_returns_error_result() -> None:
    """A failed wakeup yields ToolResult(is_error=True) — no exception."""
    def wakeup():
        raise BundleWakeupError("MCP server spawn failed: ENOENT npx")

    stub = LazyBundleStubTool(
        plugin_id="plug",
        server_name="memory",
        decl=BundleMcpToolDecl(name="read_file"),
        wakeup_fn=wakeup,
        registry_lookup=lambda name: None,
    )
    import asyncio
    result = asyncio.run(stub.execute(ToolCall(id="c1", name="x", arguments={})))
    assert result.is_error
    assert "spawn failed" in result.content.lower() or "wakeup" in result.content.lower()


def test_stub_tool_no_real_tool_after_wakeup_returns_error() -> None:
    """If wakeup runs but no real tool surfaces, return clear error."""
    wakeup_called: list[bool] = []

    def wakeup():
        wakeup_called.append(True)  # but no tool registered

    stub = LazyBundleStubTool(
        plugin_id="plug",
        server_name="memory",
        decl=BundleMcpToolDecl(name="read_file"),
        wakeup_fn=wakeup,
        registry_lookup=lambda name: None,
    )
    import asyncio
    result = asyncio.run(stub.execute(ToolCall(id="c1", name="x", arguments={})))
    assert result.is_error
    assert "not registered" in result.content.lower() or (
        "wakeup" in result.content.lower()
    )

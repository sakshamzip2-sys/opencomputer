"""Hermes parity G8: mcp_<server>_<tool> naming alongside <server>__<tool>."""
from __future__ import annotations

import pytest

from opencomputer.mcp.client import MCPTool


def _make_tool(server: str, tool: str) -> MCPTool:
    return MCPTool(
        server_name=server,
        tool_name=tool,
        description="test",
        parameters={"type": "object", "properties": {}},
        session=None,
    )


def test_canonical_name_unchanged():
    t = _make_tool("filesystem", "read_file")
    assert t.schema.name == "filesystem__read_file"


def test_hermes_alias_helper_produces_spec_name():
    """The helper produces the Hermes-spec ``mcp_<server>_<tool>`` form."""
    from opencomputer.mcp.client import hermes_alias_name
    assert hermes_alias_name("filesystem", "read_file") == "mcp_filesystem_read_file"
    # Hyphens preserved (gh's `create-issue` becomes `mcp_github_create-issue`)
    assert hermes_alias_name("github", "create-issue") == "mcp_github_create-issue"


def test_alias_tool_publishes_spec_name_and_forwards():
    """The alias tool publishes the Hermes-spec name + delegates execute."""
    from opencomputer.mcp.client import MCPAliasTool

    canonical = _make_tool("fs", "list")
    alias = MCPAliasTool(canonical)
    assert alias.schema.name == "mcp_fs_list"
    assert alias.schema.description == canonical.schema.description
    assert alias.schema.parameters == canonical.schema.parameters
    # Same canonical reference
    assert alias._canonical is canonical

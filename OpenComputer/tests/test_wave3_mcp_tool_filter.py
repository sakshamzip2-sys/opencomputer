"""Wave 3 — per-server MCP tools_allow / tools_deny filter."""

from __future__ import annotations

from opencomputer.agent.config import MCPServerConfig
from opencomputer.mcp.client import _passes_tool_filter


def test_no_filters_pass_all():
    cfg = MCPServerConfig(name="srv")
    assert _passes_tool_filter("read_file", cfg)
    assert _passes_tool_filter("anything", cfg)


def test_tools_allow_whitelist():
    cfg = MCPServerConfig(name="srv", tools_allow=("read_file", "grep"))
    assert _passes_tool_filter("read_file", cfg)
    assert _passes_tool_filter("grep", cfg)
    assert not _passes_tool_filter("write_file", cfg)


def test_tools_deny_blacklist():
    cfg = MCPServerConfig(name="srv", tools_deny=("write_file",))
    assert _passes_tool_filter("read_file", cfg)
    assert not _passes_tool_filter("write_file", cfg)


def test_tools_allow_empty_means_deny_all():
    """Empty allow-list = nothing matches, deny-all (intuitive reading)."""
    cfg = MCPServerConfig(name="srv", tools_allow=())
    assert not _passes_tool_filter("anything", cfg)


def test_tools_deny_overrides_allow():
    """If a tool is in both allow and deny, deny wins."""
    cfg = MCPServerConfig(
        name="srv",
        tools_allow=("a", "b", "c"),
        tools_deny=("b",),
    )
    assert _passes_tool_filter("a", cfg)
    assert not _passes_tool_filter("b", cfg)
    assert _passes_tool_filter("c", cfg)
    assert not _passes_tool_filter("d", cfg)


def test_default_mcp_server_config_has_no_filter():
    cfg = MCPServerConfig(name="srv")
    assert cfg.tools_allow is None
    assert cfg.tools_deny == ()


def test_mcp_server_config_yaml_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        """
mcp:
  servers:
    - name: filesystem
      transport: stdio
      command: mcp-server-filesystem
      tools_allow:
        - read_file
        - list_directory
      tools_deny:
        - write_file
""",
        encoding="utf-8",
    )
    from opencomputer.agent.config_store import load_config

    cfg = load_config(cfg_path)
    assert len(cfg.mcp.servers) == 1
    s = cfg.mcp.servers[0]
    assert s.name == "filesystem"
    assert s.tools_allow == ("read_file", "list_directory")
    assert s.tools_deny == ("write_file",)

"""Hermes parity G10: per-server timeout / connect_timeout."""
from __future__ import annotations

from opencomputer.agent.config import MCPServerConfig


def test_defaults_30s():
    cfg = MCPServerConfig(name="x")
    assert cfg.timeout == 30.0
    assert cfg.connect_timeout == 30.0


def test_can_set_per_server():
    cfg = MCPServerConfig(name="x", timeout=5.0, connect_timeout=10.0)
    assert cfg.timeout == 5.0
    assert cfg.connect_timeout == 10.0


def test_normalize_keeps_timeouts():
    """G10: timeouts pass through the per-server normalizer unchanged."""
    from opencomputer.agent.config_store import _normalize_mcp_server_dict

    raw = {"name": "x", "timeout": 5, "connect_timeout": 15}
    out = _normalize_mcp_server_dict(raw)
    assert out["timeout"] == 5
    assert out["connect_timeout"] == 15


def test_mcptool_carries_timeout():
    """MCPTool should expose the configured tool-call timeout for downstream wrappers."""
    from opencomputer.mcp.client import MCPTool

    t = MCPTool(
        server_name="s",
        tool_name="t",
        description="x",
        parameters={"type": "object", "properties": {}},
        session=None,
        timeout=7.5,
    )
    assert t.timeout == 7.5


def test_mcptool_default_timeout_30s():
    """MCPTool ctor without timeout = 30s default."""
    from opencomputer.mcp.client import MCPTool

    t = MCPTool(
        server_name="s",
        tool_name="t",
        description="x",
        parameters={"type": "object", "properties": {}},
        session=None,
    )
    assert t.timeout == 30.0

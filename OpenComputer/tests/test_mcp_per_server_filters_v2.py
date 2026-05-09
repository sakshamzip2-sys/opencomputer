"""Hermes parity G9: per-server prompts_enabled / resources_enabled + nested-form YAML."""
from __future__ import annotations

from opencomputer.agent.config import MCPServerConfig


def test_default_both_enabled():
    cfg = MCPServerConfig(name="x")
    assert cfg.prompts_enabled is True
    assert cfg.resources_enabled is True


def test_can_disable_prompts():
    cfg = MCPServerConfig(name="x", prompts_enabled=False)
    assert cfg.prompts_enabled is False


def test_can_disable_resources():
    cfg = MCPServerConfig(name="x", resources_enabled=False)
    assert cfg.resources_enabled is False


def test_normalize_helper_maps_nested_tools_form():
    """Hermes-spec nested form: tools.include / tools.exclude / prompts: false."""
    from opencomputer.agent.config_store import _normalize_mcp_server_dict

    yaml_dict = {
        "name": "github",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-github"],
        "tools": {
            "include": ["create_issue", "list_issues"],
            "prompts": False,
            "resources": False,
        },
    }
    normalized = _normalize_mcp_server_dict(yaml_dict)
    assert normalized["tools_allow"] == ["create_issue", "list_issues"]
    assert normalized["prompts_enabled"] is False
    assert normalized["resources_enabled"] is False
    # Original "tools" key removed after normalization
    assert "tools" not in normalized


def test_normalize_helper_passes_through_flat_form():
    """Flat OC-native form is left unchanged."""
    from opencomputer.agent.config_store import _normalize_mcp_server_dict

    yaml_dict = {
        "name": "x",
        "tools_allow": ["a", "b"],
        "tools_deny": ["c"],
        "prompts_enabled": False,
    }
    out = _normalize_mcp_server_dict(yaml_dict)
    assert out == yaml_dict


def test_normalize_helper_handles_partial_nested_form():
    """Only some keys nested → only those normalized; others passed through."""
    from opencomputer.agent.config_store import _normalize_mcp_server_dict

    yaml_dict = {
        "name": "x",
        "tools": {"exclude": ["dangerous_tool"]},
    }
    out = _normalize_mcp_server_dict(yaml_dict)
    assert out["tools_deny"] == ["dangerous_tool"]
    # Defaults left out — caller will use dataclass defaults
    assert "prompts_enabled" not in out
    assert "resources_enabled" not in out

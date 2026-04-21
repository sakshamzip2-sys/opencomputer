"""Phase 4 tests: MCP config + manager + bundled skills discovery."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

# ─── Config: MCP section ────────────────────────────────────────


def test_mcp_section_defaults_empty() -> None:
    from opencomputer.agent.config import default_config

    cfg = default_config()
    assert cfg.mcp.servers == ()
    assert cfg.mcp.deferred is True


def test_mcp_section_loaded_from_yaml(tmp_path: Path) -> None:
    from opencomputer.agent.config_store import load_config

    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        """
mcp:
  deferred: false
  servers:
    - name: investor-agent
      transport: stdio
      command: python3
      args:
        - -m
        - investor_agent.server
      enabled: true
    - name: stockflow
      transport: stdio
      command: python3
      args:
        - /Users/saksham/mcp-servers/mcp-stockflow/stockflow.py
""",
        encoding="utf-8",
    )
    cfg = load_config(config_file)
    assert cfg.mcp.deferred is False
    assert len(cfg.mcp.servers) == 2
    assert cfg.mcp.servers[0].name == "investor-agent"
    assert cfg.mcp.servers[0].command == "python3"
    assert cfg.mcp.servers[0].args == ("-m", "investor_agent.server")
    assert cfg.mcp.servers[1].name == "stockflow"


def test_mcp_config_roundtrip_yaml(tmp_path: Path) -> None:
    from dataclasses import replace

    from opencomputer.agent.config import (
        MCPConfig,
        MCPServerConfig,
        default_config,
    )
    from opencomputer.agent.config_store import load_config, save_config

    cfg = default_config()
    new_mcp = MCPConfig(
        servers=(
            MCPServerConfig(
                name="test-server",
                command="python3",
                args=("-m", "test"),
            ),
        ),
        deferred=True,
    )
    new_cfg = replace(cfg, mcp=new_mcp)

    config_file = tmp_path / "cfg.yaml"
    save_config(new_cfg, config_file)
    reloaded = load_config(config_file)

    assert len(reloaded.mcp.servers) == 1
    assert reloaded.mcp.servers[0].name == "test-server"
    assert reloaded.mcp.servers[0].args == ("-m", "test")


# ─── MCPTool / MCPManager ───────────────────────────────────────


def test_mcp_tool_schema_namespacing() -> None:
    from opencomputer.mcp.client import MCPTool

    mock_session = MagicMock()
    t = MCPTool(
        server_name="investor-agent",
        tool_name="get_quote",
        description="Get a stock quote",
        parameters={"type": "object", "properties": {"ticker": {"type": "string"}}},
        session=mock_session,
    )
    # Tool names get namespaced so two servers can safely expose the same tool
    assert t.schema.name == "investor-agent__get_quote"
    assert "stock quote" in t.schema.description.lower()


def test_mcp_tool_execute_returns_text_content() -> None:
    from opencomputer.mcp.client import MCPTool
    from plugin_sdk.core import ToolCall

    # Mock an MCP session that returns a text content block
    text_block = MagicMock()
    text_block.text = "AAPL is $150"
    mock_result = MagicMock()
    mock_result.content = [text_block]
    mock_result.isError = False

    mock_session = MagicMock()
    mock_session.call_tool = AsyncMock(return_value=mock_result)

    t = MCPTool(
        server_name="x",
        tool_name="y",
        description="",
        parameters={"type": "object"},
        session=mock_session,
    )
    result = asyncio.run(
        t.execute(ToolCall(id="1", name="x__y", arguments={"ticker": "AAPL"}))
    )
    assert result.content == "AAPL is $150"
    assert not result.is_error


def test_mcp_tool_execute_handles_errors() -> None:
    from opencomputer.mcp.client import MCPTool
    from plugin_sdk.core import ToolCall

    mock_session = MagicMock()
    mock_session.call_tool = AsyncMock(side_effect=RuntimeError("connection lost"))

    t = MCPTool(
        server_name="x",
        tool_name="y",
        description="",
        parameters={"type": "object"},
        session=mock_session,
    )
    result = asyncio.run(t.execute(ToolCall(id="1", name="x__y", arguments={})))
    assert result.is_error
    assert "connection lost" in result.content


# ─── Bundled skills ─────────────────────────────────────────────


def test_bundled_skill_is_discoverable() -> None:
    """The skill we shipped in opencomputer/skills/ should appear in list_skills."""
    from opencomputer.agent.memory import MemoryManager

    with tempfile.TemporaryDirectory() as tmp:
        # User skills path is empty — so only bundled should be found
        mm = MemoryManager(
            declarative_path=Path(tmp) / "MEMORY.md",
            skills_path=Path(tmp) / "skills",
        )
        found = mm.list_skills()
        ids = [s.id for s in found]
        assert "debug-python-import-error" in ids


def test_user_skill_shadows_bundled(tmp_path: Path) -> None:
    """A user-created skill with the same id as a bundled skill wins."""
    from opencomputer.agent.memory import MemoryManager

    user_skills = tmp_path / "skills"
    user_skills.mkdir()
    (user_skills / "debug-python-import-error").mkdir()
    (user_skills / "debug-python-import-error" / "SKILL.md").write_text(
        "---\nname: User override\ndescription: custom user description\n---\nuser body",
        encoding="utf-8",
    )

    mm = MemoryManager(
        declarative_path=tmp_path / "MEMORY.md",
        skills_path=user_skills,
    )
    found = mm.list_skills()
    override = next(s for s in found if s.id == "debug-python-import-error")
    assert override.name == "User override"
    assert "custom user description" in override.description

"""Tests for agent.disabled_toolsets — prefix-match tool deny (Hermes config v2).

Hermes spec: ``agent.disabled_toolsets: ["memory", "web"]`` removes any
tool whose name starts with one of the listed prefixes (with ``_`` boundary
or exact match). Existing exact-match ``tools.deny`` continues to work
in parallel — both filters compose.
"""
from __future__ import annotations

from pathlib import Path

import pytest


def test_default_disabled_toolsets_empty() -> None:
    from opencomputer.agent.config import default_config

    cfg = default_config()
    assert cfg.loop.disabled_toolsets == ()


def test_load_config_parses_disabled_toolsets(tmp_path: Path) -> None:
    from opencomputer.agent.config_store import load_config

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "loop:\n  disabled_toolsets:\n    - memory\n    - web\n",
        encoding="utf-8",
    )
    cfg = load_config(cfg_path)
    assert cfg.loop.disabled_toolsets == ("memory", "web")


def test_set_deny_prefixes_filters_at_register() -> None:
    """A tool registered with a name matching a deny prefix is skipped."""
    from plugin_sdk.tool_contract import BaseTool, ToolSchema
    from plugin_sdk.core import ToolCall, ToolResult
    from opencomputer.tools.registry import ToolRegistry

    class _FakeTool(BaseTool):
        def __init__(self, name: str) -> None:
            self._name = name

        @property
        def schema(self) -> ToolSchema:
            return ToolSchema(name=self._name, description="x", parameters={})

        async def execute(self, call: ToolCall) -> ToolResult:  # pragma: no cover
            return ToolResult(tool_call_id=call.id, content="")

    reg = ToolRegistry()
    reg.set_deny_prefixes(("memory", "web"))
    reg.register(_FakeTool("memory_save"))
    reg.register(_FakeTool("memory_search"))
    reg.register(_FakeTool("web_search"))
    reg.register(_FakeTool("execute_code"))
    reg.register(_FakeTool("memory"))  # exact match also denied

    names = [t.schema.name for t in reg.all_tools()]
    assert "execute_code" in names
    assert "memory_save" not in names
    assert "memory_search" not in names
    assert "web_search" not in names
    assert "memory" not in names


def test_no_disabled_keeps_all() -> None:
    from plugin_sdk.tool_contract import BaseTool, ToolSchema
    from plugin_sdk.core import ToolCall, ToolResult
    from opencomputer.tools.registry import ToolRegistry

    class _FakeTool(BaseTool):
        def __init__(self, name: str) -> None:
            self._name = name

        @property
        def schema(self) -> ToolSchema:
            return ToolSchema(name=self._name, description="x", parameters={})

        async def execute(self, call: ToolCall) -> ToolResult:  # pragma: no cover
            return ToolResult(tool_call_id=call.id, content="")

    reg = ToolRegistry()
    reg.set_deny_prefixes(())
    reg.register(_FakeTool("memory_save"))
    reg.register(_FakeTool("execute_code"))

    names = [t.schema.name for t in reg.all_tools()]
    assert len(names) == 2


def test_is_denied_prefix_check() -> None:
    """``is_denied_prefix`` mirrors ``is_denied`` for prefix patterns."""
    from opencomputer.tools.registry import ToolRegistry

    reg = ToolRegistry()
    reg.set_deny_prefixes(("memory", "web"))
    assert reg.is_denied_prefix("memory_save")
    assert reg.is_denied_prefix("memory_search")
    assert reg.is_denied_prefix("web_search")
    assert reg.is_denied_prefix("memory")  # exact prefix match
    assert not reg.is_denied_prefix("execute_code")
    assert not reg.is_denied_prefix("memorial_helper")  # not _-bounded


def test_compose_with_existing_exact_denylist() -> None:
    """Both ``set_denylist`` (exact) and ``set_deny_prefixes`` filter."""
    from plugin_sdk.tool_contract import BaseTool, ToolSchema
    from plugin_sdk.core import ToolCall, ToolResult
    from opencomputer.tools.registry import ToolRegistry

    class _FakeTool(BaseTool):
        def __init__(self, name: str) -> None:
            self._name = name

        @property
        def schema(self) -> ToolSchema:
            return ToolSchema(name=self._name, description="x", parameters={})

        async def execute(self, call: ToolCall) -> ToolResult:  # pragma: no cover
            return ToolResult(tool_call_id=call.id, content="")

    reg = ToolRegistry()
    reg.set_denylist(("execute_code",))   # exact match deny
    reg.set_deny_prefixes(("memory",))     # prefix deny
    reg.register(_FakeTool("memory_save"))     # filtered by prefix
    reg.register(_FakeTool("execute_code"))    # filtered by exact
    reg.register(_FakeTool("web_search"))      # passes both

    names = [t.schema.name for t in reg.all_tools()]
    assert names == ["web_search"]

"""Tests for the ToolRegistry denylist short-circuit."""

from __future__ import annotations

from opencomputer.tools.registry import ToolRegistry
from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema


class _StubTool(BaseTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="StubTool",
            description="x",
            parameters={"type": "object", "properties": {}},
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        return ToolResult(tool_call_id=call.id, content="ok")


def _fresh_registry() -> ToolRegistry:
    """Build a clean registry instance for isolation per test."""
    return ToolRegistry()


def test_register_normal_tool_succeeds():
    r = _fresh_registry()
    r.register(_StubTool())
    assert r.get("StubTool") is not None


def test_denied_tool_is_skipped_at_registration():
    r = _fresh_registry()
    r.set_denylist(["StubTool"])
    r.register(_StubTool())
    assert r.get("StubTool") is None


def test_is_denied_helper():
    r = _fresh_registry()
    r.set_denylist(["StubTool", "OtherTool"])
    assert r.is_denied("StubTool") is True
    assert r.is_denied("OtherTool") is True
    assert r.is_denied("NotDenied") is False


def test_denylist_is_case_sensitive():
    """openclaw convention: exact-name match. 'stubtool' != 'StubTool'."""
    r = _fresh_registry()
    r.set_denylist(["stubtool"])
    r.register(_StubTool())
    assert r.get("StubTool") is not None  # still registered


def test_denylist_clear_resets():
    r = _fresh_registry()
    r.set_denylist(["StubTool"])
    assert r.is_denied("StubTool") is True
    r.set_denylist([])
    assert r.is_denied("StubTool") is False


def test_register_after_deny_then_clear_is_idempotent():
    """Re-registering after clearing the denylist works (no stale state)."""
    r = _fresh_registry()
    r.set_denylist(["StubTool"])
    r.register(_StubTool())  # silently skipped
    r.set_denylist([])
    r.register(_StubTool())  # now succeeds
    assert r.get("StubTool") is not None


def test_tools_config_has_deny_field():
    """agent.tools.deny — list[str], default empty."""
    from opencomputer.agent.config import ToolsConfig

    cfg = ToolsConfig()
    assert hasattr(cfg, "deny")
    assert cfg.deny == ()

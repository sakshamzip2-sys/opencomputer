"""PR-4: tests for DelegateTool MAX_DEPTH + BLOCKED_TOOLS safety."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from opencomputer.tools.delegate import DELEGATE_BLOCKED_TOOLS, DelegateTool
from plugin_sdk.core import ToolCall
from plugin_sdk.runtime_context import RuntimeContext


def _make_child_loop():
    """Return a fake child loop whose run_conversation is an AsyncMock."""
    child_loop = MagicMock()
    child_loop.config = MagicMock()
    # dataclasses.is_dataclass(MagicMock()) is False, so the budget-override
    # branch is skipped — keeps the fixture simple.
    child_loop.run_conversation = AsyncMock()
    child_result = MagicMock()
    child_result.final_message.content = "OK"
    child_result.session_id = "child-session"
    child_loop.run_conversation.return_value = child_result
    return child_loop


def _set_factory(depth_cap: int = 2):
    """Wire DelegateTool with a fake parent loop carrying a config."""
    parent_loop = MagicMock()
    parent_loop.config.loop.max_delegation_depth = depth_cap
    child_loop = _make_child_loop()
    child_loop.config = parent_loop.config
    factory = MagicMock(return_value=child_loop)
    factory.__self__ = parent_loop
    DelegateTool.set_factory(factory)
    return parent_loop, child_loop


def setup_function():
    """Reset DelegateTool class-level state before each test."""
    DelegateTool._factory = None
    DelegateTool._current_runtime = RuntimeContext()
    DelegateTool._templates = {}


@pytest.mark.asyncio
async def test_delegate_refuses_at_max_depth():
    """At depth >= max_depth, delegate returns is_error=True."""
    parent_loop, child_loop = _set_factory(depth_cap=2)
    DelegateTool.set_runtime(RuntimeContext(delegation_depth=2))
    tool = DelegateTool()
    result = await tool.execute(ToolCall(id="c1", name="delegate", arguments={"task": "do thing"}))
    assert result.is_error
    assert "max delegation depth" in result.content
    child_loop.run_conversation.assert_not_called()


@pytest.mark.asyncio
async def test_delegate_at_depth_one_allowed():
    """At depth=1 with cap=2, delegate succeeds (1 < 2)."""
    parent_loop, child_loop = _set_factory(depth_cap=2)
    DelegateTool.set_runtime(RuntimeContext(delegation_depth=1))
    tool = DelegateTool()
    result = await tool.execute(ToolCall(id="c1", name="delegate", arguments={"task": "do thing"}))
    assert not result.is_error
    child_loop.run_conversation.assert_called_once()


@pytest.mark.asyncio
async def test_delegate_increments_depth_for_child():
    """Child runtime has depth = parent.depth + 1."""
    parent_loop, child_loop = _set_factory(depth_cap=5)
    DelegateTool.set_runtime(RuntimeContext(delegation_depth=0))
    tool = DelegateTool()
    await tool.execute(ToolCall(id="c1", name="delegate", arguments={"task": "do"}))
    # Inspect what was passed as runtime to child
    call_kwargs = child_loop.run_conversation.call_args.kwargs
    assert call_kwargs["runtime"].delegation_depth == 1


@pytest.mark.asyncio
async def test_delegate_increments_depth_from_nonzero():
    """Depth increments correctly from an already-positive starting depth."""
    parent_loop, child_loop = _set_factory(depth_cap=5)
    DelegateTool.set_runtime(RuntimeContext(delegation_depth=3))
    tool = DelegateTool()
    await tool.execute(ToolCall(id="c1", name="delegate", arguments={"task": "do"}))
    call_kwargs = child_loop.run_conversation.call_args.kwargs
    assert call_kwargs["runtime"].delegation_depth == 4


@pytest.mark.asyncio
async def test_delegate_blocks_explicit_blocked_tool_in_allowlist():
    """`allowed_tools=['delegate']` is a hard error — recursive delegation prevention."""
    parent_loop, child_loop = _set_factory(depth_cap=2)
    DelegateTool.set_runtime(RuntimeContext(delegation_depth=0))
    tool = DelegateTool()
    result = await tool.execute(ToolCall(
        id="c1", name="delegate",
        arguments={"task": "do thing", "allowed_tools": ["delegate", "Read"]}
    ))
    assert result.is_error
    assert "blocked tools" in result.content.lower()
    assert "delegate" in result.content
    child_loop.run_conversation.assert_not_called()


@pytest.mark.asyncio
async def test_delegate_blocks_askuserquestion_in_allowlist():
    """`allowed_tools=['AskUserQuestion']` is a hard error — subagent has no user."""
    parent_loop, child_loop = _set_factory(depth_cap=2)
    DelegateTool.set_runtime(RuntimeContext(delegation_depth=0))
    tool = DelegateTool()
    result = await tool.execute(ToolCall(
        id="c1", name="delegate",
        arguments={"task": "do thing", "allowed_tools": ["AskUserQuestion"]}
    ))
    assert result.is_error
    assert "AskUserQuestion" in result.content


@pytest.mark.asyncio
async def test_delegate_blocks_exitplanmode_in_allowlist():
    """`allowed_tools=['ExitPlanMode']` is a hard error — subagent doesn't own plan mode."""
    parent_loop, child_loop = _set_factory(depth_cap=2)
    DelegateTool.set_runtime(RuntimeContext(delegation_depth=0))
    tool = DelegateTool()
    result = await tool.execute(ToolCall(
        id="c1", name="delegate",
        arguments={"task": "do thing", "allowed_tools": ["ExitPlanMode"]}
    ))
    assert result.is_error
    assert "ExitPlanMode" in result.content


@pytest.mark.asyncio
async def test_delegate_allows_safe_explicit_allowlist():
    """An allowlist with no blocked tools succeeds."""
    parent_loop, child_loop = _set_factory(depth_cap=2)
    DelegateTool.set_runtime(RuntimeContext(delegation_depth=0))
    tool = DelegateTool()
    result = await tool.execute(ToolCall(
        id="c1", name="delegate",
        arguments={"task": "do thing", "allowed_tools": ["Read", "Bash"]}
    ))
    assert not result.is_error
    child_loop.run_conversation.assert_called_once()


@pytest.mark.asyncio
async def test_delegate_depth_zero_with_default_cap_passes():
    """Depth 0 with default cap 2 must succeed (normal parent-level call)."""
    parent_loop, child_loop = _set_factory(depth_cap=2)
    DelegateTool.set_runtime(RuntimeContext(delegation_depth=0))
    tool = DelegateTool()
    result = await tool.execute(ToolCall(id="c1", name="delegate", arguments={"task": "do"}))
    assert not result.is_error


@pytest.mark.asyncio
async def test_delegate_custom_depth_cap_respected():
    """A depth_cap of 1 means depth=1 is already at the limit and should be rejected."""
    parent_loop, child_loop = _set_factory(depth_cap=1)
    DelegateTool.set_runtime(RuntimeContext(delegation_depth=1))
    tool = DelegateTool()
    result = await tool.execute(ToolCall(id="c1", name="delegate", arguments={"task": "do"}))
    assert result.is_error
    assert "max delegation depth" in result.content
    child_loop.run_conversation.assert_not_called()


def test_delegate_blocked_tools_constant_includes_known_unsafe():
    """The DELEGATE_BLOCKED_TOOLS constant lists at minimum the safety-critical tools."""
    assert "delegate" in DELEGATE_BLOCKED_TOOLS
    assert "AskUserQuestion" in DELEGATE_BLOCKED_TOOLS
    assert "ExitPlanMode" in DELEGATE_BLOCKED_TOOLS

"""PR-E: DelegateTool integration with file-coordination."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from opencomputer.tools.delegate import DelegateTool
from opencomputer.tools.delegation_coordinator import reset_default_coordinator
from plugin_sdk.core import ToolCall
from plugin_sdk.runtime_context import RuntimeContext


def _set_delegate_factory():
    """Plumb DelegateTool with a fake parent loop."""
    parent_loop = MagicMock()
    parent_loop.config.loop.max_delegation_depth = 5
    child_loop = MagicMock()
    child_loop.config = parent_loop.config
    child_loop.run_conversation = AsyncMock()
    child_result = MagicMock()
    child_result.final_message.content = "OK"
    child_result.session_id = "child-session"
    child_loop.run_conversation.return_value = child_result
    factory = MagicMock(return_value=child_loop)
    factory.__self__ = parent_loop
    DelegateTool.set_factory(factory)
    return parent_loop, child_loop


@pytest.fixture(autouse=True)
def _reset_state():
    reset_default_coordinator()
    yield
    reset_default_coordinator()


@pytest.mark.asyncio
async def test_delegate_with_no_paths_unchanged():
    """Existing behavior preserved when 'paths' is omitted."""
    parent, child = _set_delegate_factory()
    DelegateTool.set_runtime(RuntimeContext(delegation_depth=0))
    tool = DelegateTool()
    result = await tool.execute(ToolCall(id="c1", name="delegate", arguments={"task": "do thing"}))
    assert not result.is_error
    child.run_conversation.assert_called_once()


@pytest.mark.asyncio
async def test_delegate_with_paths_acquires_locks():
    """When 'paths' is provided, the delegate runs inside acquired locks."""
    parent, child = _set_delegate_factory()
    DelegateTool.set_runtime(RuntimeContext(delegation_depth=0))
    tool = DelegateTool()
    result = await tool.execute(ToolCall(
        id="c1", name="delegate",
        arguments={"task": "edit file", "paths": ["/tmp/edit_target.py"]},
    ))
    assert not result.is_error
    child.run_conversation.assert_called_once()


@pytest.mark.asyncio
async def test_delegate_paths_must_be_list():
    """Non-list paths arg returns error."""
    parent, child = _set_delegate_factory()
    DelegateTool.set_runtime(RuntimeContext(delegation_depth=0))
    tool = DelegateTool()
    result = await tool.execute(ToolCall(
        id="c1", name="delegate",
        arguments={"task": "do thing", "paths": "not-a-list"},
    ))
    assert result.is_error
    assert "must be a list" in result.content
    child.run_conversation.assert_not_called()


def test_delegate_schema_includes_paths():
    """Tool schema documents the new 'paths' parameter."""
    tool = DelegateTool()
    schema = tool.schema
    props = schema.parameters.get("properties", {})
    assert "paths" in props
    assert props["paths"]["type"] == "array"

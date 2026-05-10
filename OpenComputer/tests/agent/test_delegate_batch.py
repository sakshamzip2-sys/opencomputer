"""Hermes parity (2026-05-08): delegate tasks=[...] parallel batch + max_concurrent."""

import os

import pytest

from opencomputer.agent.config import LoopConfig
from opencomputer.tools.delegate import DelegateTool
from plugin_sdk.core import ToolCall


def test_loop_config_max_concurrent_children_default():
    cfg = LoopConfig()
    assert cfg.max_concurrent_children == 3


def test_loop_config_child_timeout_default():
    cfg = LoopConfig()
    assert cfg.child_timeout_seconds == 600


def test_loop_config_overrides_apply():
    cfg = LoopConfig(max_concurrent_children=10, child_timeout_seconds=300)
    assert cfg.max_concurrent_children == 10
    assert cfg.child_timeout_seconds == 300


def test_delegate_schema_includes_tasks():
    schema = DelegateTool().schema
    props = schema.parameters["properties"]
    assert "tasks" in props
    # `task` no longer required when tasks=[...] is supplied
    assert schema.parameters["required"] == []


@pytest.mark.asyncio
async def test_batch_with_too_many_tasks_returns_error_not_truncate(monkeypatch):
    monkeypatch.setenv("DELEGATION_MAX_CONCURRENT_CHILDREN", "3")
    tool = DelegateTool()
    call = ToolCall(
        id="c1",
        name="delegate",
        arguments={
            "tasks": [
                {"goal": "task 1"},
                {"goal": "task 2"},
                {"goal": "task 3"},
                {"goal": "task 4"},
                {"goal": "task 5"},
            ],
        },
    )
    result = await tool.execute(call)
    assert result.is_error is True
    assert "exceeds" in result.content
    assert "max_concurrent_children=3" in result.content


@pytest.mark.asyncio
async def test_batch_and_single_goal_mutually_exclusive():
    tool = DelegateTool()
    call = ToolCall(
        id="c1",
        name="delegate",
        arguments={"task": "x", "tasks": [{"goal": "y"}]},
    )
    result = await tool.execute(call)
    assert result.is_error is True
    assert "either" in result.content.lower()


@pytest.mark.asyncio
async def test_batch_empty_list_returns_error():
    tool = DelegateTool()
    call = ToolCall(
        id="c1",
        name="delegate",
        arguments={"tasks": []},
    )
    result = await tool.execute(call)
    assert result.is_error is True
    assert "non-empty" in result.content


@pytest.mark.asyncio
async def test_batch_invalid_tasks_type_returns_error():
    tool = DelegateTool()
    call = ToolCall(
        id="c1",
        name="delegate",
        arguments={"tasks": "not a list"},
    )
    result = await tool.execute(call)
    assert result.is_error is True


@pytest.mark.asyncio
async def test_env_var_overrides_config_cap(monkeypatch):
    """DELEGATION_MAX_CONCURRENT_CHILDREN env var beats LoopConfig field."""
    monkeypatch.setenv("DELEGATION_MAX_CONCURRENT_CHILDREN", "1")
    tool = DelegateTool()
    call = ToolCall(
        id="c1",
        name="delegate",
        arguments={
            "tasks": [{"goal": "a"}, {"goal": "b"}],
        },
    )
    result = await tool.execute(call)
    # Cap=1, batch=2 → reject
    assert result.is_error is True
    assert "max_concurrent_children=1" in result.content


@pytest.mark.asyncio
async def test_batch_invalid_env_cap_falls_back_to_default(monkeypatch):
    """Garbage env var doesn't crash; falls back to default cap."""
    monkeypatch.setenv("DELEGATION_MAX_CONCURRENT_CHILDREN", "not-an-int")
    tool = DelegateTool()
    call = ToolCall(
        id="c1",
        name="delegate",
        arguments={
            # 5 > 3 (default) so this should error
            "tasks": [{"goal": str(i)} for i in range(5)],
        },
    )
    result = await tool.execute(call)
    assert result.is_error is True
    assert "max_concurrent_children=3" in result.content

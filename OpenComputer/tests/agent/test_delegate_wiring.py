"""Hermes parity (2026-05-08): verify DelegateTool wires DelegationConfig
into the child loop's model and registers/updates SubagentRegistry."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from opencomputer.agent.config import (
    DelegationConfig,
    LoopConfig,
    ModelConfig,
    default_config,
)
from opencomputer.agent.subagent_registry import SubagentRegistry
from opencomputer.tools.delegate import DelegateTool
from plugin_sdk.core import ToolCall


@pytest.fixture(autouse=True)
def _reset_registry():
    SubagentRegistry.instance().reset()
    yield
    SubagentRegistry.instance().reset()


def _build_fake_subagent_loop(*, success_text: str = "ok"):
    """A minimal subagent_loop double whose .config is a real Config and
    whose run_conversation returns a Result-shaped object."""
    cfg = default_config()
    fake = MagicMock()
    fake.config = cfg
    fake.allowed_tools = None
    fake_msg = MagicMock(content=success_text)
    fake_result = MagicMock(final_message=fake_msg, session_id="sess-123")
    fake.run_conversation = AsyncMock(return_value=fake_result)
    return fake


def _build_parent_loop_with_delegation(delegation: DelegationConfig | None):
    """A parent_loop double with a frozen Config carrying the delegation override."""
    cfg = default_config()
    if delegation is not None:
        new_loop = dataclasses.replace(cfg.loop, delegation=delegation)
        cfg = dataclasses.replace(cfg, loop=new_loop)
    parent = MagicMock()
    parent.config = cfg
    return parent


@pytest.mark.asyncio
async def test_delegation_override_applies_model_and_provider():
    """DelegationConfig.{model,provider} should rewrite child's ModelConfig."""
    parent = _build_parent_loop_with_delegation(
        DelegationConfig(model="gemini-2.5-flash", provider="openrouter")
    )
    child_loop = _build_fake_subagent_loop()

    tool = DelegateTool()
    factory = MagicMock(return_value=child_loop)
    factory.__self__ = parent
    DelegateTool.set_factory(factory, instance=tool)

    call = ToolCall(id="c1", name="delegate", arguments={"task": "test goal"})
    result = await tool.execute(call)

    # Assert override applied
    assert child_loop.config.model.model == "gemini-2.5-flash"
    assert child_loop.config.model.provider == "openrouter"
    assert result.is_error is False or result.is_error is None


@pytest.mark.asyncio
async def test_delegation_override_none_inherits_parent():
    """All-None DelegationConfig leaves child's ModelConfig untouched."""
    parent = _build_parent_loop_with_delegation(DelegationConfig())
    child_loop = _build_fake_subagent_loop()
    original_model = child_loop.config.model.model
    original_provider = child_loop.config.model.provider

    tool = DelegateTool()
    factory = MagicMock(return_value=child_loop)
    factory.__self__ = parent
    DelegateTool.set_factory(factory, instance=tool)

    call = ToolCall(id="c2", name="delegate", arguments={"task": "test goal"})
    await tool.execute(call)

    assert child_loop.config.model.model == original_model
    assert child_loop.config.model.provider == original_provider


@pytest.mark.asyncio
async def test_delegation_override_partial_only_replaces_set_fields():
    """Only DelegationConfig.model set → provider should still inherit."""
    parent = _build_parent_loop_with_delegation(
        DelegationConfig(model="claude-haiku-4-5-20251001")  # provider unset
    )
    child_loop = _build_fake_subagent_loop()
    original_provider = child_loop.config.model.provider

    tool = DelegateTool()
    factory = MagicMock(return_value=child_loop)
    factory.__self__ = parent
    DelegateTool.set_factory(factory, instance=tool)

    call = ToolCall(id="c3", name="delegate", arguments={"task": "test"})
    await tool.execute(call)

    assert child_loop.config.model.model == "claude-haiku-4-5-20251001"
    assert child_loop.config.model.provider == original_provider


@pytest.mark.asyncio
async def test_subagent_registers_in_registry():
    """A successful delegation should leave a 'completed' record in the
    registry's history."""
    parent = _build_parent_loop_with_delegation(None)
    child_loop = _build_fake_subagent_loop(success_text="all done")

    tool = DelegateTool()
    factory = MagicMock(return_value=child_loop)
    factory.__self__ = parent
    DelegateTool.set_factory(factory, instance=tool)

    call = ToolCall(id="c4", name="delegate", arguments={"task": "explore the docs"})
    await tool.execute(call)

    history = SubagentRegistry.instance().history()
    assert len(history) == 1
    assert history[0].state == "completed"
    assert history[0].goal == "explore the docs"
    assert history[0].ended_at is not None


@pytest.mark.asyncio
async def test_subagent_registry_records_failure_on_exception():
    """When the child's run_conversation raises, the registry records 'failed'."""
    parent = _build_parent_loop_with_delegation(None)
    child_loop = _build_fake_subagent_loop()
    child_loop.run_conversation = AsyncMock(
        side_effect=RuntimeError("provider auth failed")
    )

    tool = DelegateTool()
    factory = MagicMock(return_value=child_loop)
    factory.__self__ = parent
    DelegateTool.set_factory(factory, instance=tool)

    call = ToolCall(id="c5", name="delegate", arguments={"task": "doomed"})
    with pytest.raises(RuntimeError, match="provider auth failed"):
        await tool.execute(call)

    history = SubagentRegistry.instance().history()
    assert len(history) == 1
    assert history[0].state == "failed"
    assert "provider auth failed" in (history[0].error or "")


@pytest.mark.asyncio
async def test_registry_register_failure_does_not_break_delegation():
    """If the registry blows up at register-time, delegation must still
    succeed (best-effort by design)."""
    parent = _build_parent_loop_with_delegation(None)
    child_loop = _build_fake_subagent_loop(success_text="resilient")

    tool = DelegateTool()
    factory = MagicMock(return_value=child_loop)
    factory.__self__ = parent
    DelegateTool.set_factory(factory, instance=tool)

    # Monkeypatch register to raise
    real_instance = SubagentRegistry.instance()
    original_register = real_instance.register

    def _boom(**_kw):
        raise RuntimeError("registry exploded")

    real_instance.register = _boom  # type: ignore[assignment]
    try:
        call = ToolCall(id="c6", name="delegate", arguments={"task": "x"})
        result = await tool.execute(call)
        assert result.content == "resilient"
    finally:
        real_instance.register = original_register  # restore

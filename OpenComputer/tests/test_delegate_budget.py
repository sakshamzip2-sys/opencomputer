"""
II.1 — Per-subagent iteration budgets.

Hermes (sources/hermes-agent/run_agent.py:IterationBudget lines 185-196)
gives the parent agent 90 iterations and subagents 50 by default. Before
this change, OpenComputer's DelegateTool spawned child AgentLoops with
the parent's full ``config.loop.max_iterations``, allowing runaway
subagent chains to burn through the parent's token budget unchecked.

These tests pin the new budget field and the DelegateTool override.
"""

from __future__ import annotations

import asyncio
import dataclasses

from opencomputer.agent.config import Config, LoopConfig
from opencomputer.tools.delegate import DelegateTool
from plugin_sdk.core import ToolCall


def test_loop_config_delegation_max_iterations_defaults_to_50() -> None:
    """LoopConfig.delegation_max_iterations exists and defaults to 50."""
    cfg = LoopConfig()
    assert cfg.delegation_max_iterations == 50


def test_loop_config_delegation_max_iterations_is_customizable() -> None:
    """LoopConfig can be constructed with a custom delegation budget."""
    cfg = LoopConfig(delegation_max_iterations=25)
    assert cfg.delegation_max_iterations == 25
    # And works via dataclasses.replace (used internally by DelegateTool).
    cfg2 = dataclasses.replace(cfg, delegation_max_iterations=10)
    assert cfg2.delegation_max_iterations == 10


def test_delegate_overrides_child_max_iterations() -> None:
    """DelegateTool swaps the child loop's ``config.loop.max_iterations``
    with the parent config's ``delegation_max_iterations`` before the
    child runs — so runaway subagents can't consume the parent budget."""

    parent_cfg = Config(
        loop=LoopConfig(max_iterations=90, delegation_max_iterations=50)
    )

    captured: dict = {}

    class _FakeLoop:
        def __init__(self) -> None:
            # Child is born with the parent's config (mirroring what the
            # CLI factory does: ``AgentLoop(provider=provider, config=cfg)``).
            self.config = parent_cfg

        async def run_conversation(self, user_message, runtime=None, **kw):
            # Snapshot the config AT THE MOMENT the child runs — this is
            # what any iteration loop inside the child would see.
            captured["max_iterations"] = self.config.loop.max_iterations
            captured["delegation_max_iterations"] = (
                self.config.loop.delegation_max_iterations
            )

            class _R:
                class final_message:
                    content = "ok"

                session_id = "sub"

            return _R()

    DelegateTool.set_factory(lambda: _FakeLoop())
    tool = DelegateTool()
    result = asyncio.run(
        tool.execute(ToolCall(id="1", name="delegate", arguments={"task": "go"}))
    )
    assert not result.is_error
    # The child's max_iterations must now reflect the parent's subagent
    # budget (50), NOT the parent's main-loop budget (90).
    assert captured["max_iterations"] == 50
    assert captured["delegation_max_iterations"] == 50


def test_delegate_override_respects_custom_delegation_budget() -> None:
    """Custom delegation_max_iterations flows through to the child loop."""
    parent_cfg = Config(
        loop=LoopConfig(max_iterations=90, delegation_max_iterations=17)
    )

    captured: dict = {}

    class _FakeLoop:
        def __init__(self) -> None:
            self.config = parent_cfg

        async def run_conversation(self, user_message, runtime=None, **kw):
            captured["max_iterations"] = self.config.loop.max_iterations

            class _R:
                class final_message:
                    content = "ok"

                session_id = "sub"

            return _R()

    DelegateTool.set_factory(lambda: _FakeLoop())
    tool = DelegateTool()
    result = asyncio.run(
        tool.execute(ToolCall(id="1", name="delegate", arguments={"task": "go"}))
    )
    assert not result.is_error
    assert captured["max_iterations"] == 17

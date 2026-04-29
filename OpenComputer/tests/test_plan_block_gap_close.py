"""Regression: /plan slash command engages the plan_block hook (PR-1 gap-close).

Today, ``/plan`` writes ``runtime.custom["plan_mode"] = True`` but
``plan_block.py`` only reads ``ctx.runtime.plan_mode`` (the frozen field) —
so the hard-block does NOT fire from ``/plan``. After PR-1 Task 3, the
hook reads through ``effective_permission_mode()`` and engages correctly.
"""

from __future__ import annotations

import pytest

from extensions.coding_harness.hooks.plan_block import (  # type: ignore[import-not-found]
    plan_mode_block_hook,
)
from plugin_sdk import PermissionMode, RuntimeContext
from plugin_sdk.core import ToolCall
from plugin_sdk.hooks import HookContext, HookEvent


def _make_ctx(runtime: RuntimeContext, tool: str = "Edit") -> HookContext:
    return HookContext(
        event=HookEvent.PRE_TOOL_USE,
        session_id="s1",
        tool_call=ToolCall(id="c1", name=tool, arguments={"file_path": "/tmp/x"}),
        runtime=runtime,
    )


@pytest.mark.asyncio
class TestPlanModeFromCustomDict:
    async def test_custom_plan_mode_blocks_edit(self) -> None:
        # Today this would NOT block (the gap). After PR-1 it must block.
        rt = RuntimeContext(custom={"plan_mode": True})
        decision = await plan_mode_block_hook(_make_ctx(rt))
        assert decision is not None
        assert decision.decision == "block"
        assert "plan mode" in decision.reason.lower()

    async def test_canonical_permission_mode_plan_blocks(self) -> None:
        rt = RuntimeContext(permission_mode=PermissionMode.PLAN)
        decision = await plan_mode_block_hook(_make_ctx(rt))
        assert decision is not None
        assert decision.decision == "block"

    async def test_default_does_not_block(self) -> None:
        rt = RuntimeContext()
        assert await plan_mode_block_hook(_make_ctx(rt)) is None

    async def test_legacy_field_still_blocks(self) -> None:
        rt = RuntimeContext(plan_mode=True)
        decision = await plan_mode_block_hook(_make_ctx(rt))
        assert decision is not None
        assert decision.decision == "block"

    async def test_canonical_custom_permission_mode_blocks(self) -> None:
        rt = RuntimeContext(custom={"permission_mode": "plan"})
        decision = await plan_mode_block_hook(_make_ctx(rt))
        assert decision is not None
        assert decision.decision == "block"

"""accept_edits_hook auto-approves Edit-family tools in ACCEPT_EDITS mode."""

from __future__ import annotations

import pytest
from extensions.coding_harness.hooks.accept_edits_hook import (  # type: ignore[import-not-found]
    accept_edits_hook,
)

from plugin_sdk import PermissionMode, RuntimeContext
from plugin_sdk.core import ToolCall
from plugin_sdk.hooks import HookContext, HookEvent


def _ctx(rt: RuntimeContext, tool: str, args: dict | None = None) -> HookContext:
    return HookContext(
        event=HookEvent.PRE_TOOL_USE,
        session_id="s1",
        tool_call=ToolCall(id="c1", name=tool, arguments=args or {}),
        runtime=rt,
    )


@pytest.mark.asyncio
class TestAcceptEditsHook:
    @pytest.fixture
    def runtime(self) -> RuntimeContext:
        return RuntimeContext(permission_mode=PermissionMode.ACCEPT_EDITS)

    @pytest.mark.parametrize("tool", ["Edit", "Write", "MultiEdit", "NotebookEdit"])
    async def test_auto_approves_edit_family(
        self, runtime: RuntimeContext, tool: str
    ) -> None:
        decision = await accept_edits_hook(_ctx(runtime, tool))
        assert decision is not None
        assert decision.decision == "approve"
        assert tool in decision.reason

    async def test_does_not_approve_bash(self, runtime: RuntimeContext) -> None:
        decision = await accept_edits_hook(
            _ctx(runtime, "Bash", {"command": "ls"})
        )
        assert decision is None

    async def test_does_not_approve_bash_sed_i(self, runtime: RuntimeContext) -> None:
        # Bash that mutates files via sed -i is NOT auto-approved.
        # Accept-edits opted in to file-edit *tools*, not arbitrary shell.
        decision = await accept_edits_hook(
            _ctx(runtime, "Bash", {"command": "sed -i 's/x/y/' file"})
        )
        assert decision is None

    async def test_does_not_approve_webfetch(self, runtime: RuntimeContext) -> None:
        decision = await accept_edits_hook(_ctx(runtime, "WebFetch"))
        assert decision is None

    async def test_does_not_approve_websearch(self, runtime: RuntimeContext) -> None:
        decision = await accept_edits_hook(_ctx(runtime, "WebSearch"))
        assert decision is None

    async def test_only_fires_in_accept_edits_mode(self) -> None:
        rt_default = RuntimeContext()
        decision = await accept_edits_hook(_ctx(rt_default, "Edit"))
        assert decision is None

    async def test_does_not_fire_in_auto_mode(self) -> None:
        # In auto mode, the BypassManager handles bypass — accept-edits is a no-op.
        rt_auto = RuntimeContext(permission_mode=PermissionMode.AUTO)
        decision = await accept_edits_hook(_ctx(rt_auto, "Edit"))
        assert decision is None

    async def test_does_not_fire_in_plan_mode(self) -> None:
        # Plan mode refuses these tools; accept-edits hook should not interfere.
        rt_plan = RuntimeContext(permission_mode=PermissionMode.PLAN)
        decision = await accept_edits_hook(_ctx(rt_plan, "Edit"))
        assert decision is None

    async def test_legacy_custom_accept_edits_via_helper(self) -> None:
        # /accept-edits writes both 'permission_mode' and legacy 'accept_edits';
        # the helper resolves to ACCEPT_EDITS via the canonical key first.
        rt = RuntimeContext(custom={"permission_mode": "accept-edits", "accept_edits": True})
        decision = await accept_edits_hook(_ctx(rt, "Edit"))
        assert decision is not None
        assert decision.decision == "approve"

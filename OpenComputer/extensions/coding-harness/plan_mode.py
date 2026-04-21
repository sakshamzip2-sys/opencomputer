"""Plan-mode dynamic injection provider + PreToolUse block hook."""

from __future__ import annotations

from plugin_sdk.core import ToolCall
from plugin_sdk.hooks import HookContext, HookDecision, HookEvent, HookSpec
from plugin_sdk.injection import DynamicInjectionProvider, InjectionContext


PLAN_MODE_TEXT = (
    "## PLAN MODE ACTIVE\n\n"
    "Describe what you would do step-by-step, but do NOT execute any destructive "
    "tool calls. The following tools are DISABLED in plan mode: Edit, Write, Bash, "
    "MultiEdit, start_process, kill_process. You MAY use Read, Grep, Glob, Todo* to "
    "investigate. End your reply with a numbered plan and wait for user confirmation."
)


#: Tool names blocked when plan_mode is active.
DESTRUCTIVE_TOOLS = frozenset({
    "Edit",
    "Write",
    "Bash",
    "MultiEdit",
    "start_process",
    "kill_process",
})


class PlanModeInjectionProvider(DynamicInjectionProvider):
    """Fires only when `runtime.plan_mode` is True."""

    priority = 10  # first in the injected system block

    @property
    def provider_id(self) -> str:
        return "coding-harness:plan-mode"

    def collect(self, ctx: InjectionContext) -> str | None:
        if ctx.runtime.plan_mode:
            return PLAN_MODE_TEXT
        return None


async def plan_mode_block_hook(ctx: HookContext) -> HookDecision | None:
    """PreToolUse hook — refuses destructive tools while plan_mode is active.

    Belt + suspenders with the injection: even if the model forgets the rule in
    the system prompt, the hook hard-stops the call.
    """
    if ctx.runtime is None or not ctx.runtime.plan_mode:
        return None
    if ctx.tool_call is None:
        return None
    if ctx.tool_call.name not in DESTRUCTIVE_TOOLS:
        return None
    return HookDecision(
        decision="block",
        reason=(
            f"plan mode active — {ctx.tool_call.name} refused. "
            f"Describe the plan instead; the user will confirm before execution."
        ),
    )


def build_plan_mode_hook_spec() -> HookSpec:
    return HookSpec(
        event=HookEvent.PRE_TOOL_USE,
        handler=plan_mode_block_hook,
        matcher=None,  # we check tool names ourselves inside the handler
        fire_and_forget=False,
    )


__all__ = [
    "PlanModeInjectionProvider",
    "plan_mode_block_hook",
    "build_plan_mode_hook_spec",
    "DESTRUCTIVE_TOOLS",
    "PLAN_MODE_TEXT",
]

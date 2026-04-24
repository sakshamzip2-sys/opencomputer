"""plan_block — PreToolUse hook that refuses destructive tools while in plan mode."""

from __future__ import annotations

from plugin_sdk.hooks import HookContext, HookDecision, HookEvent, HookSpec

DESTRUCTIVE_TOOLS = frozenset(
    {
        "Edit",
        "Write",
        "Bash",
        "MultiEdit",
        "StartProcess",
        "KillProcess",
    }
)


async def plan_mode_block_hook(ctx: HookContext) -> HookDecision | None:
    if ctx.runtime is None or not ctx.runtime.plan_mode:
        return None
    if ctx.tool_call is None or ctx.tool_call.name not in DESTRUCTIVE_TOOLS:
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
        matcher=None,
        fire_and_forget=False,
    )


__all__ = [
    "DESTRUCTIVE_TOOLS",
    "plan_mode_block_hook",
    "build_plan_mode_hook_spec",
]

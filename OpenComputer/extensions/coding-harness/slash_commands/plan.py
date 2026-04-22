"""/plan and /plan-off slash commands.

`/plan` toggles plan mode on by mutating `runtime.custom["plan_mode"]` — the
core RuntimeContext is frozen, so modes that need per-turn toggling read from
`runtime.custom` instead of the top-level flag. PlanModeInjectionProvider
honours both `runtime.plan_mode` and `runtime.custom["plan_mode"]` when the
core loop is plumbed (Phase 6f core work).
"""

from __future__ import annotations

from .base import SlashCommand


class PlanOnCommand(SlashCommand):
    name = "plan"
    description = "Enable plan mode (read-only planning; destructive tools refused)."

    async def execute(self, args: str, runtime, harness_ctx) -> str:
        runtime.custom["plan_mode"] = True
        harness_ctx.session_state.set("mode:plan", True)
        return (
            "Plan mode enabled. Destructive tools will be refused. "
            "Describe your plan and the user will confirm before execution."
        )


class PlanOffCommand(SlashCommand):
    name = "plan-off"
    description = "Disable plan mode and allow destructive tool calls again."

    async def execute(self, args: str, runtime, harness_ctx) -> str:
        runtime.custom["plan_mode"] = False
        harness_ctx.session_state.set("mode:plan", False)
        return "Plan mode disabled."


__all__ = ["PlanOnCommand", "PlanOffCommand"]

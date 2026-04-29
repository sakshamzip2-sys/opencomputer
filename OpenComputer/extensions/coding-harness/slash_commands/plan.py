"""/plan and /plan-off slash commands.

`/plan` toggles plan mode on by mutating `runtime.custom["plan_mode"]` — the
core RuntimeContext is frozen, so modes that need per-turn toggling read from
`runtime.custom` instead of the top-level flag. PlanModeInjectionProvider
honours both `runtime.plan_mode` and `runtime.custom["plan_mode"]` when the
core loop is plumbed (Phase 6f core work).

Phase 12b6 D8: formally subclasses ``plugin_sdk.SlashCommand`` and returns
``SlashCommandResult``; harness context is captured in ``__init__``.
"""

from __future__ import annotations

from typing import Any

from .base import SlashCommand, SlashCommandResult


class PlanOnCommand(SlashCommand):
    name = "plan"
    description = "Enable plan mode (read-only planning; destructive tools refused)."

    async def execute(self, args: str, runtime: Any) -> SlashCommandResult:
        runtime.custom["permission_mode"] = "plan"  # canonical
        runtime.custom["plan_mode"] = True           # legacy mirror
        runtime.custom["yolo_session"] = False        # exclusive with auto
        self.harness_ctx.session_state.set("mode:plan", True)
        return SlashCommandResult(
            output=(
                "Plan mode enabled. Destructive tools will be refused. "
                "Describe your plan and the user will confirm before execution."
            ),
            handled=True,
        )


class PlanOffCommand(SlashCommand):
    name = "plan-off"
    description = "Disable plan mode and allow destructive tool calls again."

    async def execute(self, args: str, runtime: Any) -> SlashCommandResult:
        # Clear canonical only if it was PLAN — don't clobber a different mode.
        if runtime.custom.get("permission_mode") == "plan":
            runtime.custom.pop("permission_mode", None)
        runtime.custom["plan_mode"] = False
        self.harness_ctx.session_state.set("mode:plan", False)
        return SlashCommandResult(output="Plan mode disabled.", handled=True)


__all__ = ["PlanOnCommand", "PlanOffCommand"]

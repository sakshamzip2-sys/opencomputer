"""/accept-edits slash command — toggle accept-edits mode.

Phase 12b6 D8: subclasses ``plugin_sdk.SlashCommand`` + returns
``SlashCommandResult``.
"""

from __future__ import annotations

from typing import Any

from .base import SlashCommand, SlashCommandResult


class AcceptEditsCommand(SlashCommand):
    name = "accept-edits"
    description = (
        "Toggle accept-edits mode. When on, small edits are auto-accepted; "
        "use /undo to revert the most recent one."
    )

    async def execute(self, args: str, runtime: Any) -> SlashCommandResult:
        current = bool(runtime.custom.get("accept_edits"))
        new = args.strip().lower() not in {"off", "0", "false", "no"} and not current
        if args.strip().lower() in {"on", "1", "true", "yes"}:
            new = True
        elif args.strip().lower() in {"off", "0", "false", "no"}:
            new = False
        runtime.custom["accept_edits"] = new
        self.harness_ctx.session_state.set("mode:accept_edits", new)
        return SlashCommandResult(
            output=f"Accept-edits mode: {'on' if new else 'off'}.",
            handled=True,
        )


__all__ = ["AcceptEditsCommand"]

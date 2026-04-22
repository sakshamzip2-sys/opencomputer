"""/accept-edits slash command — toggle accept-edits mode."""

from __future__ import annotations

from .base import SlashCommand


class AcceptEditsCommand(SlashCommand):
    name = "accept-edits"
    description = (
        "Toggle accept-edits mode. When on, small edits are auto-accepted; "
        "use /undo to revert the most recent one."
    )

    async def execute(self, args: str, runtime, harness_ctx) -> str:
        current = bool(runtime.custom.get("accept_edits"))
        new = args.strip().lower() not in {"off", "0", "false", "no"} and not current
        if args.strip().lower() in {"on", "1", "true", "yes"}:
            new = True
        elif args.strip().lower() in {"off", "0", "false", "no"}:
            new = False
        runtime.custom["accept_edits"] = new
        harness_ctx.session_state.set("mode:accept_edits", new)
        return f"Accept-edits mode: {'on' if new else 'off'}."


__all__ = ["AcceptEditsCommand"]

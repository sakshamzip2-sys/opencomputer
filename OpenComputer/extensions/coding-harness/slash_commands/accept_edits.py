"""/accept-edits slash command — toggle accept-edits mode.

Phase 12b6 D8: subclasses ``plugin_sdk.SlashCommand`` + returns
``SlashCommandResult``.

PR-3 (2026-04-29): writes the canonical ``runtime.custom["permission_mode"]``
key alongside the legacy ``runtime.custom["accept_edits"]`` bool so the
unified :func:`plugin_sdk.effective_permission_mode` resolver agrees with
the older injection-provider read path. The hook
:mod:`hooks.accept_edits_hook` also reads through the helper.
"""

from __future__ import annotations

from typing import Any

from .base import SlashCommand, SlashCommandResult


class AcceptEditsCommand(SlashCommand):
    name = "accept-edits"
    description = (
        "Toggle accept-edits mode. Edit/Write/MultiEdit/NotebookEdit auto-approve; "
        "Bash and network calls still prompt. Use /undo to revert the most recent edit."
    )

    async def execute(self, args: str, runtime: Any) -> SlashCommandResult:
        current = (
            runtime.custom.get("permission_mode") == "accept-edits"
            or bool(runtime.custom.get("accept_edits"))
        )
        new = args.strip().lower() not in {"off", "0", "false", "no"} and not current
        if args.strip().lower() in {"on", "1", "true", "yes"}:
            new = True
        elif args.strip().lower() in {"off", "0", "false", "no"}:
            new = False
        if new:
            runtime.custom["permission_mode"] = "accept-edits"
            runtime.custom["accept_edits"] = True
            # Don't clobber a different active mode key.
            runtime.custom["plan_mode"] = False
            runtime.custom["yolo_session"] = False
        else:
            if runtime.custom.get("permission_mode") == "accept-edits":
                runtime.custom.pop("permission_mode", None)
            runtime.custom["accept_edits"] = False
        self.harness_ctx.session_state.set("mode:accept_edits", new)
        return SlashCommandResult(
            output=f"Accept-edits mode: {'on' if new else 'off'}.",
            handled=True,
        )


__all__ = ["AcceptEditsCommand"]

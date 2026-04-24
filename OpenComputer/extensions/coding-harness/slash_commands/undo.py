"""/undo slash command — rewind one (or N) checkpoints.

Phase 12b6 D8: subclasses ``plugin_sdk.SlashCommand`` + returns
``SlashCommandResult``.
"""

from __future__ import annotations

from typing import Any

from .base import SlashCommand, SlashCommandResult


class UndoCommand(SlashCommand):
    name = "undo"
    description = (
        "Rewind the last checkpoint (or N with `/undo 3`). Files on disk are "
        "restored to their state at that checkpoint."
    )

    async def execute(self, args: str, runtime: Any) -> SlashCommandResult:
        n = 1
        stripped = args.strip()
        if stripped:
            try:
                n = max(1, int(stripped))
            except ValueError:
                return SlashCommandResult(
                    output=f"Bad argument {stripped!r} — /undo takes an integer.",
                    handled=True,
                )
        checkpoints = self.harness_ctx.rewind_store.list()
        if not checkpoints:
            return SlashCommandResult(
                output="Nothing to undo — no checkpoints yet.", handled=True
            )
        if n > len(checkpoints):
            return SlashCommandResult(
                output=f"Only {len(checkpoints)} checkpoint(s) exist, can't undo {n}.",
                handled=True,
            )
        target = checkpoints[n - 1]
        self.harness_ctx.rewind_store.restore(target.id)
        return SlashCommandResult(
            output=(
                f"Rewound {n} checkpoint(s). Restored {target.id} "
                f"({target.label}) — {len(target.files)} file(s) rolled back."
            ),
            handled=True,
        )


__all__ = ["UndoCommand"]

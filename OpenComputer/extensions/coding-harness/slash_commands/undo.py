"""/undo slash command — rewind one (or N) checkpoints."""

from __future__ import annotations

from .base import SlashCommand


class UndoCommand(SlashCommand):
    name = "undo"
    description = (
        "Rewind the last checkpoint (or N with `/undo 3`). Files on disk are "
        "restored to their state at that checkpoint."
    )

    async def execute(self, args: str, runtime, harness_ctx) -> str:
        n = 1
        stripped = args.strip()
        if stripped:
            try:
                n = max(1, int(stripped))
            except ValueError:
                return f"Bad argument {stripped!r} — /undo takes an integer."
        checkpoints = harness_ctx.rewind_store.list()
        if not checkpoints:
            return "Nothing to undo — no checkpoints yet."
        if n > len(checkpoints):
            return f"Only {len(checkpoints)} checkpoint(s) exist, can't undo {n}."
        target = checkpoints[n - 1]
        harness_ctx.rewind_store.restore(target.id)
        return (
            f"Rewound {n} checkpoint(s). Restored {target.id} "
            f"({target.label}) — {len(target.files)} file(s) rolled back."
        )


__all__ = ["UndoCommand"]

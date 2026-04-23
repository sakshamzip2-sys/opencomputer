"""/checkpoint slash command — manually save a named checkpoint of edited files."""

from __future__ import annotations

from pathlib import Path

from rewind.checkpoint import Checkpoint  # type: ignore[import-not-found]

from .base import SlashCommand


class CheckpointCommand(SlashCommand):
    name = "checkpoint"
    description = (
        "Save a manual checkpoint of all tracked edited files. Takes an "
        "optional label — e.g. `/checkpoint before-refactor`."
    )

    async def execute(self, args: str, runtime, harness_ctx) -> str:
        label = args.strip() or "manual"
        edited: list[str] = (
            harness_ctx.session_state.get("edited_files", []) or []
        )
        files: dict[str, bytes] = {}
        for rel in edited:
            p = Path(rel)
            if p.exists() and p.is_file():
                files[rel] = p.read_bytes()
        if not files:
            return (
                "No edited files to checkpoint yet. "
                "Make some edits first — the harness will auto-track them."
            )
        cp = Checkpoint.from_files(files, label=label)
        await harness_ctx.rewind_store.save_shielded(cp)
        return (
            f"Checkpoint saved: {cp.id} ({label}). "
            f"{len(files)} file(s) snapshotted."
        )


__all__ = ["CheckpointCommand"]

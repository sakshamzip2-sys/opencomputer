"""/checkpoint slash command — manually save a named checkpoint of edited files.

Phase 12b6 D8: subclasses ``plugin_sdk.SlashCommand`` + returns
``SlashCommandResult``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rewind.checkpoint import Checkpoint  # type: ignore[import-not-found]

from .base import SlashCommand, SlashCommandResult


class CheckpointCommand(SlashCommand):
    name = "checkpoint"
    description = (
        "Save a manual checkpoint of all tracked edited files. Takes an "
        "optional label — e.g. `/checkpoint before-refactor`."
    )

    async def execute(self, args: str, runtime: Any) -> SlashCommandResult:
        label = args.strip() or "manual"
        edited: list[str] = (
            self.harness_ctx.session_state.get("edited_files", []) or []
        )
        files: dict[str, bytes] = {}
        for rel in edited:
            p = Path(rel)
            if p.exists() and p.is_file():
                files[rel] = p.read_bytes()
        if not files:
            return SlashCommandResult(
                output=(
                    "No edited files to checkpoint yet. "
                    "Make some edits first — the harness will auto-track them."
                ),
                handled=True,
            )
        cp = Checkpoint.from_files(files, label=label)
        await self.harness_ctx.rewind_store.save_shielded(cp)
        return SlashCommandResult(
            output=(
                f"Checkpoint saved: {cp.id} ({label}). "
                f"{len(files)} file(s) snapshotted."
            ),
            handled=True,
        )


__all__ = ["CheckpointCommand"]

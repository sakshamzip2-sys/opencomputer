"""/diff slash command — unified diff vs latest (or specified) checkpoint.

Self-contained (no plugin-local tool dependency) so the command is importable
in any test harness regardless of sys.path state.
"""

from __future__ import annotations

import difflib

from .base import SlashCommand


def _decode(b: bytes) -> str:
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b.decode("latin-1")


class DiffCommand(SlashCommand):
    name = "diff"
    description = (
        "Show a unified diff of the working tree vs the most recent checkpoint. "
        "Optional checkpoint id argument: `/diff <checkpoint_id>`."
    )

    async def execute(self, args: str, runtime, harness_ctx) -> str:
        checkpoints = harness_ctx.rewind_store.list()
        if not checkpoints:
            return "No checkpoints to diff against."

        cp_id = args.strip() or None
        if cp_id:
            target = next((c for c in checkpoints if c.id == cp_id), None)
            if target is None:
                return f"No checkpoint {cp_id}."
        else:
            target = checkpoints[0]

        lines: list[str] = []
        for rel_path, old_bytes in target.files.items():
            live = harness_ctx.rewind_store.workspace_root / rel_path
            new_bytes = live.read_bytes() if live.exists() and live.is_file() else b""
            old_text = _decode(old_bytes)
            new_text = _decode(new_bytes)
            if old_text == new_text:
                continue
            lines.extend(
                difflib.unified_diff(
                    old_text.splitlines(keepends=True),
                    new_text.splitlines(keepends=True),
                    fromfile=f"a/{rel_path}@{target.id}",
                    tofile=f"b/{rel_path}",
                )
            )
        if not lines:
            return f"No changes since checkpoint {target.id}."
        return "".join(lines)


__all__ = ["DiffCommand"]

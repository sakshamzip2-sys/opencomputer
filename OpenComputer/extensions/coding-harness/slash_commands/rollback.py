"""``/rollback`` slash command — Hermes Tier 2.A port for OC's checkpoint store.

OC's existing ``/undo [N]`` rewinds to the N-th-most-recent checkpoint.
This adds the *list* + *diff* + (explicit) *restore* surface Hermes ships
on its `/rollback` command — the missing UX gap.

Subcommands::

    /rollback                 → list (default; same as /rollback list)
    /rollback list            → numbered list of checkpoints with labels
    /rollback diff <N>        → show changed paths between current state
                                and the N-th-most-recent checkpoint
    /rollback restore <N>     → rewind to the N-th-most-recent checkpoint
                                (same effect as /undo N — kept for parity)
    /rollback <N>             → bare integer = restore (Hermes-compat)

Mirrors hermes_cli/checkpoint_manager.py:402-525 list/diff/restore methods
adapted to OC's RewindStore. Lives next to /undo (which is unchanged).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import SlashCommand, SlashCommandResult


class RollbackCommand(SlashCommand):
    name = "rollback"
    description = (
        "List / diff / rewind checkpoints. "
        "Use `/rollback list` to see them, `/rollback diff 1` to see what "
        "the last checkpoint changed, `/rollback 1` to restore."
    )

    async def execute(self, args: str, runtime: Any) -> SlashCommandResult:
        tokens = args.strip().split()
        if not tokens:
            return self._handle_list()

        sub = tokens[0].lower()

        # Bare integer N → restore (Hermes-compat)
        if sub.isdigit():
            return self._handle_restore(int(sub))

        if sub == "list":
            return self._handle_list()
        if sub == "diff":
            if len(tokens) < 2 or not tokens[1].isdigit():
                return SlashCommandResult(
                    output=(
                        "Usage: /rollback diff <N>  "
                        "(N is the index from /rollback list)"
                    ),
                    handled=True,
                )
            return self._handle_diff(int(tokens[1]))
        if sub == "restore":
            if len(tokens) < 2 or not tokens[1].isdigit():
                return SlashCommandResult(
                    output="Usage: /rollback restore <N>",
                    handled=True,
                )
            return self._handle_restore(int(tokens[1]))

        return SlashCommandResult(
            output=(
                f"Unknown subcommand: /rollback {sub}  "
                "(try: list | diff <N> | restore <N> | <N>)"
            ),
            handled=True,
        )

    # ─── subcommand handlers ────────────────────────────────────

    def _handle_list(self) -> SlashCommandResult:
        checkpoints = self.harness_ctx.rewind_store.list()
        if not checkpoints:
            return SlashCommandResult(
                output="No checkpoints yet. Make some edits or run /checkpoint.",
                handled=True,
            )
        lines = [f"Checkpoints ({len(checkpoints)} total, newest first):"]
        for i, cp in enumerate(checkpoints, start=1):
            label = cp.label or "(unlabeled)"
            n_files = len(cp.files)
            lines.append(f"  {i}. {cp.id[:8]}  [{label}]  ({n_files} file(s))")
        return SlashCommandResult(output="\n".join(lines), handled=True)

    def _handle_restore(self, n: int) -> SlashCommandResult:
        if n < 1:
            return SlashCommandResult(
                output="Index must be ≥1.", handled=True
            )
        checkpoints = self.harness_ctx.rewind_store.list()
        if not checkpoints:
            return SlashCommandResult(
                output="No checkpoints to restore.", handled=True
            )
        if n > len(checkpoints):
            return SlashCommandResult(
                output=(
                    f"Only {len(checkpoints)} checkpoint(s) exist, "
                    f"can't restore index {n}."
                ),
                handled=True,
            )
        target = checkpoints[n - 1]
        self.harness_ctx.rewind_store.restore(target.id)
        return SlashCommandResult(
            output=(
                f"Rolled back to checkpoint {n} ({target.id[:8]}, "
                f"label={target.label!r}). "
                f"{len(target.files)} file(s) restored."
            ),
            handled=True,
        )

    def _handle_diff(self, n: int) -> SlashCommandResult:
        if n < 1:
            return SlashCommandResult(
                output="Index must be ≥1.", handled=True
            )
        checkpoints = self.harness_ctx.rewind_store.list()
        if not checkpoints:
            return SlashCommandResult(
                output="No checkpoints to diff against.", handled=True
            )
        if n > len(checkpoints):
            return SlashCommandResult(
                output=(
                    f"Only {len(checkpoints)} checkpoint(s) exist, "
                    f"can't diff index {n}."
                ),
                handled=True,
            )
        target = checkpoints[n - 1]
        ws_root = self.harness_ctx.rewind_store.workspace_root
        # Compute path-level diff: which paths in the checkpoint differ
        # from the current on-disk state? We don't render full unified
        # diffs (that's the Edit-tool's job); just summarize what's
        # changed/missing/added relative to the checkpoint.
        changed: list[str] = []
        missing: list[str] = []
        same: list[str] = []
        for rel, data in target.files.items():
            current_path = Path(ws_root) / rel
            if not current_path.exists():
                missing.append(rel)
                continue
            try:
                if current_path.read_bytes() == data:
                    same.append(rel)
                else:
                    changed.append(rel)
            except OSError:
                missing.append(rel)

        lines = [
            f"Diff vs checkpoint {n} ({target.id[:8]}, label={target.label!r}):"
        ]
        if changed:
            lines.append(f"  changed ({len(changed)}):")
            for p in sorted(changed):
                lines.append(f"    M  {p}")
        if missing:
            lines.append(f"  missing on disk ({len(missing)}):")
            for p in sorted(missing):
                lines.append(f"    -  {p}")
        if not changed and not missing:
            lines.append("  no differences — current state matches checkpoint.")
        return SlashCommandResult(output="\n".join(lines), handled=True)


__all__ = ["RollbackCommand"]

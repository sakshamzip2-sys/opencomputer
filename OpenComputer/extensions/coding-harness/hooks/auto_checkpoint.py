"""auto_checkpoint — PreToolUse hook that snapshots files before destructive calls.

Snapshots are written to the shared `RewindStore` via `save_shielded()` so a
Ctrl-C mid-write cannot corrupt the snapshot. The hook never blocks — it only
records state. The plan-mode hook (separate, in `plan_block.py`) is what does
the actual blocking.
"""

from __future__ import annotations

from pathlib import Path

from rewind.checkpoint import Checkpoint  # type: ignore[import-not-found]

from plugin_sdk.hooks import HookContext, HookDecision, HookEvent, HookSpec

DESTRUCTIVE_TOOLS = frozenset({"Edit", "MultiEdit", "Write", "Bash"})


def _extract_candidate_path(args: dict) -> str | None:
    for key in ("path", "file", "file_path", "target_file"):
        val = args.get(key)
        if isinstance(val, str):
            return val
    return None


def build_auto_checkpoint_hook_spec(*, harness_ctx) -> HookSpec:
    async def handler(ctx: HookContext) -> HookDecision | None:
        if ctx.tool_call is None:
            return None
        if ctx.tool_call.name not in DESTRUCTIVE_TOOLS:
            return None

        edited: list[str] = (
            harness_ctx.session_state.get("edited_files", []) or []
        )
        candidate = _extract_candidate_path(ctx.tool_call.arguments)
        paths = set(edited)
        if candidate:
            paths.add(candidate)

        files: dict[str, bytes] = {}
        for rel in paths:
            p = Path(rel)
            if p.exists() and p.is_file():
                try:
                    files[rel] = p.read_bytes()
                except OSError:
                    pass
        if not files:
            return None

        cp = Checkpoint.from_files(files, label=f"before {ctx.tool_call.name}")
        await harness_ctx.rewind_store.save_shielded(cp)

        if candidate and candidate not in edited:
            edited.append(candidate)
            harness_ctx.session_state.set("edited_files", edited)

        return None

    return HookSpec(
        event=HookEvent.PRE_TOOL_USE,
        handler=handler,
        matcher=None,
        fire_and_forget=False,
    )


__all__ = ["build_auto_checkpoint_hook_spec", "DESTRUCTIVE_TOOLS"]

"""auto_checkpoint — PreToolUse hook that snapshots files before destructive calls.

Snapshots are written to the shared :class:`RewindStore` via
``save_shielded()`` so a Ctrl-C mid-write cannot corrupt the snapshot.
The hook also kicks off an auto-prune sweep on first fire per process
(respecting ``min_interval_hours``). The hook never blocks — it only
records state. The plan-mode hook (separate, in ``plan_block.py``) is
what does the actual blocking.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from rewind.checkpoint import Checkpoint  # type: ignore[import-not-found]

from plugin_sdk.hooks import HookContext, HookDecision, HookEvent, HookSpec

logger = logging.getLogger("coding_harness.rewind.prune")

DESTRUCTIVE_TOOLS = frozenset({"Edit", "MultiEdit", "Write", "Bash"})


def _extract_candidate_path(args: dict) -> str | None:
    for key in ("path", "file", "file_path", "target_file"):
        val = args.get(key)
        if isinstance(val, str):
            return val
    return None


def _config_or_defaults() -> dict:
    """Read ``CheckpointsConfig`` defaults if available, otherwise return safe values."""
    try:
        from opencomputer.agent.config import default_config

        cp = default_config().checkpoints
        return {
            "enabled": cp.enabled,
            "auto_prune": cp.auto_prune,
            "min_interval_hours": cp.min_interval_hours,
            "max_snapshots": cp.max_snapshots,
            "max_total_size_mb": cp.max_total_size_mb,
            "max_file_size_mb": cp.max_file_size_mb,
            "retention_days": cp.retention_days,
            "delete_orphans": cp.delete_orphans,
        }
    except Exception:  # noqa: BLE001
        return {
            "enabled": True,
            "auto_prune": True,
            "min_interval_hours": 24,
            "max_snapshots": 50,
            "max_total_size_mb": 1000,
            "max_file_size_mb": 50,
            "retention_days": 30,
            "delete_orphans": True,
        }


async def _background_prune(store, cfg: dict) -> None:
    """Run :meth:`RewindStore.prune` in a thread; swallow + log on failure."""
    try:
        await asyncio.to_thread(
            store.prune,
            older_than_days=cfg["retention_days"],
            max_total_bytes=cfg["max_total_size_mb"] * 1024 * 1024,
            max_count=cfg["max_snapshots"],
            delete_orphans=cfg["delete_orphans"],
            dry_run=False,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("auto-prune failed (non-fatal): %s", exc)


def build_auto_checkpoint_hook_spec(*, harness_ctx) -> HookSpec:
    cfg = _config_or_defaults()

    async def handler(ctx: HookContext) -> HookDecision | None:
        if ctx.tool_call is None:
            return None
        if ctx.tool_call.name not in DESTRUCTIVE_TOOLS:
            return None

        # ── Decide prune intent EAGERLY (before save). We mark the
        # auto-prune slot consumed so concurrent handlers don't race;
        # the actual prune work is deferred until AFTER save lands so
        # the two operations don't fight over the store directory.
        schedule_prune = False
        if cfg["enabled"] and cfg["auto_prune"]:
            try:
                if harness_ctx.rewind_store.should_auto_prune(
                    min_interval_hours=cfg["min_interval_hours"],
                ):
                    harness_ctx.rewind_store.mark_pruned()
                    schedule_prune = True
            except Exception as exc:  # noqa: BLE001
                logger.warning("auto-prune scheduling failed (non-fatal): %s", exc)

        # ── Save phase (synchronous wrt this coroutine). ──
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

        if files:
            cp = Checkpoint.from_files(
                files,
                label=f"before {ctx.tool_call.name}",
                max_file_size_bytes=cfg["max_file_size_mb"] * 1024 * 1024,
            )
            await harness_ctx.rewind_store.save_shielded(cp)

            if candidate and candidate not in edited:
                edited.append(candidate)
                harness_ctx.session_state.set("edited_files", edited)

        # ── Schedule prune ONLY after save completes. ──
        if schedule_prune:
            try:
                asyncio.create_task(
                    _background_prune(harness_ctx.rewind_store, cfg)
                )
            except RuntimeError:
                # No running loop (rare; e.g. some unit-test contexts).
                # Falling back to synchronous run keeps the contract:
                # if we marked pruned, prune actually happens.
                await _background_prune(harness_ctx.rewind_store, cfg)

        return None

    return HookSpec(
        event=HookEvent.PRE_TOOL_USE,
        handler=handler,
        matcher=None,
        fire_and_forget=False,
    )


__all__ = ["build_auto_checkpoint_hook_spec", "DESTRUCTIVE_TOOLS"]

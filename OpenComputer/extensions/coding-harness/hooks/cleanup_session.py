"""cleanup_session — SessionEnd hook that prunes old harness state (10% prob).

Probabilistic sweep to avoid running an expensive scan every session. Deletes
session directories under `~/.opencomputer/harness/` older than the retention
window (7 days by default).
"""

from __future__ import annotations

import random
import shutil
import time
from pathlib import Path

from plugin_sdk.hooks import HookContext, HookDecision, HookEvent, HookSpec

HARNESS_ROOT = Path.home() / ".opencomputer" / "harness"
RETENTION_SECONDS = 7 * 24 * 60 * 60  # 7 days


def _do_sweep(roots: list[Path], now: float, retention: int) -> int:
    """Internal — return count of dirs removed. Pure for testability."""
    removed = 0
    for p in roots:
        if not p.exists() or not p.is_dir():
            continue
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if now - mtime > retention:
            shutil.rmtree(p, ignore_errors=True)
            removed += 1
    return removed


def build_cleanup_session_hook_spec(
    *, probability: float = 0.1, force: bool = False
) -> HookSpec:
    async def handler(ctx: HookContext) -> HookDecision | None:
        if not force and random.random() > probability:
            return None
        if not HARNESS_ROOT.exists():
            return None
        roots = [p for p in HARNESS_ROOT.iterdir() if p.is_dir()]
        _do_sweep(roots, now=time.time(), retention=RETENTION_SECONDS)
        return None

    return HookSpec(
        event=HookEvent.SESSION_END,
        handler=handler,
        matcher=None,
        fire_and_forget=True,
    )


__all__ = [
    "_do_sweep",
    "build_cleanup_session_hook_spec",
    "HARNESS_ROOT",
    "RETENTION_SECONDS",
]

"""Pre-task hook handler — Phase 2 stub.

Real query/inject logic lands in Phase 4. This stub exists so the plugin
can register against ``HookEvent.BEFORE_TASK`` from day one, the loop
seam stays exercised, and the on-disk state machine works end-to-end
before any network traffic flows.

Phase 2 contract:

* Hook fires for every turn (after ``USER_PROMPT_SUBMIT``, before the
  first LLM call — see ``opencomputer/agent/loop.py``).
* Handler reads ``state.is_enabled(profile_home)``. Disabled → return
  ``HookDecision(decision="pass")`` immediately (zero overhead).
* Enabled → still return ``"pass"`` for now (Phase 4 will replace this
  with the real query path), but write a heartbeat so the operator can
  confirm the hook is firing.

The runtime flag ``runtime.custom["trace_used"]`` is set to ``None``
even on the disabled path so the post-task subscriber sees a uniform
shape (``trace_used`` either is the string trace_id or ``None``).
"""

from __future__ import annotations

import logging
from pathlib import Path

from plugin_sdk.hooks import HookContext, HookDecision

from . import state

_log = logging.getLogger("opencomputer.social_traces.prefetch")


def _profile_home_from_runtime(ctx: HookContext) -> Path | None:
    """Best-effort profile-home resolver.

    Phase 2 reads from ``runtime.custom["profile_home"]`` if present,
    otherwise falls back to the OC default-profile path. Phase 4 will
    swap this for an explicit injection at plugin-load time so the
    hook never has to guess.
    """
    if ctx.runtime is None:
        return None
    explicit = ctx.runtime.custom.get("profile_home") if ctx.runtime.custom else None
    if explicit:
        return Path(explicit)
    try:
        from opencomputer.agent.config import _home as _profile_home_fn
        return _profile_home_fn()
    except Exception:  # noqa: BLE001 — never raise from a hook
        _log.debug("profile_home resolver failed", exc_info=True)
        return None


async def on_before_task(ctx: HookContext) -> HookDecision:
    """BEFORE_TASK hook handler.

    Phase 2: respect the on-disk enabled flag, write a heartbeat, return
    ``pass``. Phase 4 replaces the body between the heartbeat and the
    return with the real query → score → inject path.
    """
    profile_home = _profile_home_from_runtime(ctx)
    if profile_home is None:
        # Can't read state without a profile home. Treat as disabled —
        # the plugin must NEVER fail-open into the network if we don't
        # know which profile we're acting on.
        return HookDecision(decision="pass")

    if not state.is_enabled(profile_home):
        return HookDecision(decision="pass")

    # Enabled but Phase 2 — heartbeat the wiring without doing real work.
    state.write_heartbeat(profile_home)

    # Mark the runtime flag explicitly so the post-task subscriber sees
    # a uniform shape (``trace_used`` is always either a string trace_id
    # or None, never missing). ``runtime.custom`` is mutated in place —
    # the loop's per-task RuntimeContext copy we receive here is the
    # right scope.
    if ctx.runtime is not None and ctx.runtime.custom is not None:
        ctx.runtime.custom["trace_used"] = None

    return HookDecision(decision="pass")


__all__ = ["on_before_task"]

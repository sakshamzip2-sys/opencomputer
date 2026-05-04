"""
Hook engine — dispatches lifecycle events to registered handlers.

Registration pattern mirrors the tool registry. Plugins call
`engine.register(HookSpec(...))` at load time. At runtime the agent
loop emits events via `engine.fire(HookEvent.X, ctx)`.

Round 2A P-1: handlers are sorted by ``(priority, registration_index)``
so lower priorities run first and FIFO is preserved within a bucket. The
engine assigns a monotonic ``_next_seq`` at register time and resorts the
per-event list once; firing is then a straight iteration.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import defaultdict

from opencomputer.hooks.runner import fire_and_forget
from plugin_sdk.hooks import HookContext, HookDecision, HookEvent, HookSpec

logger = logging.getLogger("opencomputer.hooks")


class HookEngine:
    """Central dispatcher for lifecycle events."""

    def __init__(self) -> None:
        # Each entry is ``(priority, seq, spec)`` so we can stable-sort by
        # the priority/sequence pair without mutating the immutable
        # ``HookSpec`` itself. ``seq`` is monotonically assigned at register
        # time; sorting by ``(priority, seq)`` gives lower-priority-first
        # with FIFO within the same priority bucket.
        self._hooks: dict[HookEvent, list[tuple[int, int, HookSpec]]] = defaultdict(
            list
        )
        self._next_seq: int = 0

    def register(self, spec: HookSpec) -> None:
        seq = self._next_seq
        self._next_seq += 1
        bucket = self._hooks[spec.event]
        bucket.append((spec.priority, seq, spec))
        # Sort once at register time so fire paths are O(n) iteration only.
        bucket.sort(key=lambda entry: (entry[0], entry[1]))

    def unregister_all(self, event: HookEvent | None = None) -> None:
        if event is None:
            self._hooks.clear()
        else:
            self._hooks[event] = []

    def _ordered_specs(self, event: HookEvent) -> list[HookSpec]:
        """Return the priority-ordered specs for ``event`` (helper for tests)."""
        return [spec for _, _, spec in self._hooks.get(event, [])]

    def _matches(self, spec: HookSpec, ctx: HookContext) -> bool:
        if spec.matcher is None:
            return True
        # Matcher is a regex over tool name (for PreToolUse / PostToolUse)
        tool_name = ""
        if ctx.tool_call:
            tool_name = ctx.tool_call.name
        elif ctx.tool_result:
            tool_name = ctx.tool_result.tool_call_id  # fallback — not ideal
        try:
            return re.search(spec.matcher, tool_name) is not None
        except re.error:
            return False

    async def fire_blocking(self, ctx: HookContext) -> HookDecision | None:
        """Fire a hook event and WAIT for decisions (for PreToolUse approvals).

        Returns the first non-pass decision, or None if all hooks passed.
        Handlers run in priority order — lower priority first.

        ``HookSpec.timeout_ms`` (when > 0) wraps the handler in
        ``asyncio.wait_for``. On timeout the engine logs a warning and
        treats the handler as ``"pass"`` (fail-open) — matching OC's
        existing hook contract (CLAUDE.md §7: a wedged hook must never
        wedge the loop).
        """
        for _, _, spec in self._hooks.get(ctx.event, []):
            if not self._matches(spec, ctx):
                continue
            try:
                if spec.timeout_ms and spec.timeout_ms > 0:
                    decision = await asyncio.wait_for(
                        spec.handler(ctx),
                        timeout=spec.timeout_ms / 1000.0,
                    )
                else:
                    decision = await spec.handler(ctx)
            except TimeoutError:
                logger.warning(
                    "Hook %s timed out after %dms — failing open (pass)",
                    getattr(spec.handler, "__qualname__", repr(spec.handler)),
                    spec.timeout_ms,
                )
                continue  # fail-open
            except Exception:  # noqa: BLE001
                logger.exception("blocking hook raised")
                continue
            if decision is None or decision.decision == "pass":
                continue
            return decision
        return None

    def fire_and_forget(self, ctx: HookContext) -> None:
        """Fire a hook event without waiting. Used for PostToolUse logging etc.

        Handlers are scheduled in priority order, but because fire-and-forget
        tasks run concurrently the runtime order is not guaranteed; the
        ``priority`` field controls SCHEDULING order only.
        """
        for _, _, spec in self._hooks.get(ctx.event, []):
            if not self._matches(spec, ctx):
                continue
            fire_and_forget(spec.handler(ctx))


engine = HookEngine()


__all__ = ["HookEngine", "engine"]

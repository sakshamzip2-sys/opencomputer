"""
Hook engine — dispatches lifecycle events to registered handlers.

Registration pattern mirrors the tool registry. Plugins call
`engine.register(HookSpec(...))` at load time. At runtime the agent
loop emits events via `engine.fire(HookEvent.X, ctx)`.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict

from opencomputer.hooks.runner import fire_and_forget
from plugin_sdk.hooks import HookContext, HookDecision, HookEvent, HookSpec

logger = logging.getLogger("opencomputer.hooks")


class HookEngine:
    """Central dispatcher for lifecycle events."""

    def __init__(self) -> None:
        self._hooks: dict[HookEvent, list[HookSpec]] = defaultdict(list)

    def register(self, spec: HookSpec) -> None:
        self._hooks[spec.event].append(spec)

    def unregister_all(self, event: HookEvent | None = None) -> None:
        if event is None:
            self._hooks.clear()
        else:
            self._hooks[event] = []

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
        """
        for spec in self._hooks.get(ctx.event, []):
            if not self._matches(spec, ctx):
                continue
            try:
                decision = await spec.handler(ctx)
            except Exception:  # noqa: BLE001
                logger.exception("blocking hook raised")
                continue
            if decision is None or decision.decision == "pass":
                continue
            return decision
        return None

    def fire_and_forget(self, ctx: HookContext) -> None:
        """Fire a hook event without waiting. Used for PostToolUse logging etc."""
        for spec in self._hooks.get(ctx.event, []):
            if not self._matches(spec, ctx):
                continue
            fire_and_forget(spec.handler(ctx))


engine = HookEngine()


__all__ = ["HookEngine", "engine"]

"""Delegate-spawn-backed hook handlers (v1.1 plan-2 M8.2, 2026-05-09).

Bridges :class:`opencomputer.agent.config.HookAgentConfig` settings
entries to async :data:`HookHandler` callables that the engine can
register.

The handler synthesises a ``delegate(task=prompt, agent=<template>,
isolation="copy")`` call so the spawned subagent's tool calls don't
mutate the parent's working tree. Per the spec:

* ``max_turns`` caps the subagent's iteration budget.
* ``timeout_seconds`` caps wall-clock.
* ``token_budget`` is advisory today — the underlying delegate flow
  doesn't yet thread a per-call token cap; we surface a WARNING when
  the prompt itself estimates over budget.
* Subagent's final message text is parsed for ``allow`` / ``block``.

Same fail-open posture: any error → ``HookDecision(decision="pass")``.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opencomputer.agent.config import HookAgentConfig
    from plugin_sdk.hooks import HookContext, HookHandler

logger = logging.getLogger("opencomputer.hooks.agent_handlers")


def _estimate_tokens(text: str) -> int:
    """Rough char/4 token estimator (mirrors prompt_handlers)."""
    return max(1, len(text) // 4 + 1)


def _parse_returns(text: str, returns_mode: str) -> tuple[str, str]:
    """Allow / block parsing (subagent text → decision)."""
    s = (text or "").strip().lower()
    if returns_mode == "block":
        if s.startswith(("block", "deny", "no", "stop")):
            return ("block", "")
        return ("pass", "")
    if s.startswith(("allow", "ok", "yes", "approve", "pass")):
        return ("pass", "")
    return ("block", "")


def make_agent_hook_handler(config: HookAgentConfig) -> HookHandler:
    """Build a HookHandler that spawns a delegate subagent."""

    async def _handler(ctx: HookContext):  # type: ignore[no-untyped-def]
        from plugin_sdk.core import ToolCall
        from plugin_sdk.hooks import HookDecision

        prompt = _render_prompt(config.prompt, ctx)
        if _estimate_tokens(prompt) > config.token_budget:
            logger.warning(
                "agent-hook: prompt estimated > token_budget=%d for event %s; "
                "failing open",
                config.token_budget,
                config.event,
            )
            return HookDecision(decision="pass")

        try:
            from opencomputer.tools.delegate import DelegateTool

            delegate_args = {
                "task": prompt,
                "agent": config.agent,
                "isolation": "copy",
            }
            call = ToolCall(
                id=f"agent-hook-{uuid.uuid4().hex[:8]}",
                name="delegate",
                arguments=delegate_args,
            )
            result = await asyncio.wait_for(
                DelegateTool().execute(call),
                timeout=config.timeout_seconds,
            )
        except TimeoutError:
            logger.warning(
                "agent-hook: subagent timed out after %.1fs on event %s; "
                "failing open",
                config.timeout_seconds,
                config.event,
            )
            return HookDecision(decision="pass")
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "agent-hook: delegate failed (%s) on event %s; failing open",
                type(exc).__name__,
                config.event,
            )
            return HookDecision(decision="pass")

        if result.is_error:
            logger.warning(
                "agent-hook: subagent returned tool error on event %s; "
                "failing open. Content: %s",
                config.event,
                (result.content or "")[:200],
            )
            return HookDecision(decision="pass")

        decision, modified = _parse_returns(result.content or "", config.returns)
        return HookDecision(decision=decision, modified_message=modified)

    return _handler


def _render_prompt(prompt: str, ctx) -> str:  # type: ignore[no-untyped-def]
    """Render the agent-hook prompt, appending event context.

    The user-supplied ``prompt:`` is the meat; we tack on a small
    machine-parseable footer so the subagent knows what it's
    evaluating.
    """
    parts = [prompt]
    tc = getattr(ctx, "tool_call", None)
    if tc is not None:
        parts.append("")
        parts.append(f"Tool being assessed: {getattr(tc, 'name', '?')}")
        args = getattr(tc, "arguments", None)
        if isinstance(args, dict) and args:
            parts.append(f"Args: {repr(args)[:400]}")
    parts.append("")
    parts.append(
        "Reply with a single word: 'allow' to permit the action, "
        "'block' to deny it. Anything else is treated as 'block' in "
        "block-mode and 'pass' in allow-mode."
    )
    return "\n".join(parts)


__all__ = ["make_agent_hook_handler"]

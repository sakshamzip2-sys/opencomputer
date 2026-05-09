"""Aux-LLM-backed hook handlers (v1.1 plan-2 M8.1, 2026-05-09).

Bridges :class:`opencomputer.agent.config.HookPromptConfig` settings
entries to async :data:`HookHandler` callables that the engine can
register.

Posture: hard caps + fail-open. A wedged or over-budget aux-LLM
call MUST NOT block the agent loop — when the call hangs, errors,
or estimates over budget, the handler returns
``HookDecision(decision="pass")`` and logs a WARNING. Mirrors the
existing shell-hook contract (see CLAUDE.md §7).
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opencomputer.agent.config import HookPromptConfig
    from plugin_sdk.hooks import HookContext, HookHandler

logger = logging.getLogger("opencomputer.hooks.prompt_handlers")


def _estimate_tokens(text: str) -> int:
    """Rough char/4 token estimator. Conservative — overcounts to err on
    the side of refusing borderline calls."""
    return max(1, len(text) // 4 + 1)


def _parse_returns(raw: str, returns_mode: str) -> tuple[str, str]:
    """Convert the LLM's reply into (decision, modified_message).

    * ``allow`` mode: text starts with allow/ok/yes → pass; else block.
    * ``block`` mode: text starts with block/no/deny → block; else pass.
    * ``score`` mode: first numeric token is injected as
      ``modified_message`` — the engine forwards this to handlers that
      consume scores. The decision stays ``pass`` so the caller's
      flow is uninterrupted.
    """
    s = (raw or "").strip().lower()
    if returns_mode == "score":
        # Strip non-numeric chars, take first numeric run
        score = ""
        for ch in s:
            if ch.isdigit() or ch in (".", "-"):
                score += ch
            elif score:
                break
        return ("pass", f"score={score}" if score else "score=")
    if returns_mode == "block":
        if s.startswith(("block", "deny", "no", "stop")):
            return ("block", "")
        return ("pass", "")
    # default: allow mode
    if s.startswith(("allow", "ok", "yes", "approve", "pass")):
        return ("pass", "")
    return ("block", "")


def make_prompt_hook_handler(config: HookPromptConfig) -> HookHandler:
    """Build a HookHandler that calls aux_llm and parses the reply."""

    async def _handler(ctx: HookContext):  # type: ignore[no-untyped-def]
        from plugin_sdk.hooks import HookDecision

        # Cheap pre-flight: estimate tokens; refuse if over budget.
        # System prompt + a tiny synthetic user message ("decide.") cover
        # most cases. Real callers can override system in YAML.
        sys_prompt = config.system
        user_msg = _build_user_message(ctx)
        estimated = _estimate_tokens(sys_prompt) + _estimate_tokens(user_msg)
        if estimated > config.token_budget:
            logger.warning(
                "prompt-hook: estimated tokens %d > budget %d for event %s; "
                "failing open (pass)",
                estimated,
                config.token_budget,
                config.event,
            )
            return HookDecision(decision="pass")

        try:
            from opencomputer.agent.aux_llm import complete_text

            model_arg = config.model if config.model and config.model != "auto" else None
            raw = await asyncio.wait_for(
                complete_text(
                    messages=[{"role": "user", "content": user_msg}],
                    system=sys_prompt,
                    max_tokens=128,
                    temperature=0.0,
                    model=model_arg,
                    use_cache=True,
                ),
                timeout=config.timeout_seconds,
            )
        except TimeoutError:
            logger.warning(
                "prompt-hook: aux LLM timed out after %.1fs on event %s; "
                "failing open",
                config.timeout_seconds,
                config.event,
            )
            return HookDecision(decision="pass")
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "prompt-hook: aux LLM unavailable (%s); failing open on event %s",
                type(exc).__name__,
                config.event,
            )
            return HookDecision(decision="pass")

        decision, modified = _parse_returns(raw, config.returns)
        return HookDecision(decision=decision, modified_message=modified)

    return _handler


def _build_user_message(ctx) -> str:  # type: ignore[no-untyped-def]
    """Render a minimal user message describing the event being assessed.

    The handler doesn't have a generic message format — it cares about
    the event name + tool name + tool args (when available). Plugins
    that want richer context can register their own HookSpec instead
    of using settings-declared prompt-hooks.
    """
    parts = [f"Event: {getattr(ctx, 'event', '<unknown>')}"]
    tc = getattr(ctx, "tool_call", None)
    if tc is not None:
        parts.append(f"Tool: {getattr(tc, 'name', '?')}")
        args = getattr(tc, "arguments", None)
        if isinstance(args, dict) and args:
            # Cap arg dump so a giant `command` doesn't blow the budget.
            preview = repr(args)[:400]
            parts.append(f"Args (truncated to 400 chars): {preview}")
    return "\n".join(parts)


__all__ = ["make_prompt_hook_handler"]

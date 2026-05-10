"""Subagent hook handler factory (v1.1 plan-2 M8.2, 2026-05-09).

Sibling to :mod:`opencomputer.hooks.prompt_handlers` and
:mod:`opencomputer.hooks.shell_handlers`. Wraps each
:class:`HookAgentConfig` in an async handler that:

1. Renders the :class:`HookContext` into a user message describing the
   tool call / event payload.
2. Synthesises a ``delegate(task=prompt + context, isolation=...)``
   ToolCall and dispatches it via :class:`DelegateTool` so the spawned
   subagent runs in a fresh isolated context.
3. Parses the subagent's final assistant message into a
   :class:`HookDecision` (``allow_block`` looks for the
   ``allow``/``block`` token; ``structured`` returns the full text as
   the advisory reason).

Fail-open contract (matches the shell + prompt hook contracts):
- Subagent exception → log WARN + ``decision="pass"``.
- ``asyncio.wait_for`` timeout → log WARN + ``decision="pass"``.
- Estimated render > token budget → refuse to spawn + log + pass.
- Empty / unparseable response → log + pass.

The whole point is the same: a wedged or expensive subagent must
never wedge or bankrupt the loop. Risk-rating hooks are advisory
by construction.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from opencomputer.agent.config import HookAgentConfig
from plugin_sdk.core import ToolCall
from plugin_sdk.hooks import HookContext, HookDecision, HookEvent, HookHandler

_log = logging.getLogger("opencomputer.hooks.agent")

_CHARS_PER_TOKEN: int = 4


@dataclass(frozen=True, slots=True)
class _RenderedRequest:
    """Subagent dispatch inputs derived from a HookContext."""

    task_text: str
    estimated_input_tokens: int


def _render_context(
    config: HookAgentConfig, ctx: HookContext
) -> _RenderedRequest:
    """Convert a HookContext + the user's prompt into a delegate task string.

    Body shape::

        <user prompt body>

        Hook event: PreToolUse
        Tool: Bash
        Args: {"command": "..."}
        Session: <id>

    The user's policy prompt is the leading block; the rendered
    HookContext appears below as structured context for the subagent.
    """
    parts: list[str] = [config.prompt.strip(), ""]
    parts.append(
        f"Hook event: {ctx.event.value if isinstance(ctx.event, HookEvent) else ctx.event}"
    )
    if ctx.tool_call is not None:
        if ctx.tool_call.name:
            parts.append(f"Tool: {ctx.tool_call.name}")
        if config.matcher:
            parts.append(f"Matcher: {config.matcher}")
        if ctx.tool_call.arguments:
            try:
                args_json = json.dumps(
                    ctx.tool_call.arguments, default=str, indent=None
                )
            except (TypeError, ValueError):
                args_json = repr(ctx.tool_call.arguments)
            if len(args_json) > 1500:
                args_json = args_json[:1500] + "...[truncated]"
            parts.append(f"Args: {args_json}")
    elif config.matcher:
        parts.append(f"Matcher: {config.matcher}")
    if ctx.session_id:
        parts.append(f"Session: {ctx.session_id}")
    task = "\n".join(parts)
    estimated = max(1, len(task) // _CHARS_PER_TOKEN)
    return _RenderedRequest(task_text=task, estimated_input_tokens=estimated)


_BLOCK_PATTERN = re.compile(r"^\s*block(?:\s*[:.\-]\s*(.+))?$", re.IGNORECASE)
_ALLOW_PATTERN = re.compile(r"^\s*(?:allow|approve|pass|ok)\s*$", re.IGNORECASE)


def _parse_response_allow_block(text: str) -> HookDecision:
    """Parse an agent response under ``returns: allow_block`` semantics.

    Walks line by line for the first ``block`` / ``allow`` token. Any
    line that's neither is fail-open.
    """
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        m = _BLOCK_PATTERN.match(stripped)
        if m is not None:
            reason = (m.group(1) or "blocked by agent hook").strip()
            return HookDecision(decision="block", reason=reason)
        if _ALLOW_PATTERN.match(stripped):
            return HookDecision(decision="pass")
        _log.debug(
            "agent hook: ambiguous response %r; passing", stripped[:120]
        )
        return HookDecision(decision="pass")
    return HookDecision(decision="pass")


def _parse_response_structured(text: str) -> HookDecision:
    """Pass the full text through as advisory reason.

    `structured` mode is for subagents that produce richer reports than
    a single-token decision. We always return ``decision="pass"`` and
    surface the text as the reason so observers can log it.
    """
    body = text.strip()
    if not body:
        return HookDecision(decision="pass")
    return HookDecision(decision="pass", reason=body[:2000])


async def _invoke_delegate(
    config: HookAgentConfig, task_text: str
) -> str:
    """Spawn a subagent via DelegateTool and return its final message text.

    Lazy imports DelegateTool and ToolCall to avoid pulling the agent
    loop into module load. The returned coroutine respects the config's
    timeout via ``asyncio.wait_for``.
    """
    from opencomputer.tools.delegate import DelegateTool  # noqa: PLC0415

    delegate = DelegateTool()
    args: dict[str, Any] = {
        "task": task_text,
        "isolation": config.isolation,
    }
    if config.agent:
        args["agent"] = config.agent
    call = ToolCall(
        id=f"agent-hook-{config.event}",
        name="delegate",
        arguments=args,
    )
    result = await asyncio.wait_for(
        delegate.execute(call),
        timeout=config.timeout_seconds,
    )
    if result.is_error:
        # Surface delegate-tool errors as a fail-open log + empty text;
        # the parser fallback decides ``pass``.
        _log.warning(
            "agent hook (%s): delegate returned is_error=True: %s",
            config.event,
            (result.content or "")[:200],
        )
        return ""
    return result.content or ""


def make_agent_hook_handler(config: HookAgentConfig) -> HookHandler:
    """Wrap a :class:`HookAgentConfig` in an async :class:`HookHandler`.

    Returned handler is suitable for direct registration via
    :class:`HookSpec`. Mirrors the shell + prompt handlers in shape:
    render-and-invoke under one ``try`` block, broad fail-open on every
    error path, never raises out.
    """

    async def _handler(ctx: HookContext) -> HookDecision:
        rendered = _render_context(config, ctx)
        if rendered.estimated_input_tokens > config.token_budget_total:
            _log.warning(
                "agent hook (%s): estimated input %d tokens > budget %d; "
                "passing without spawning subagent",
                config.event,
                rendered.estimated_input_tokens,
                config.token_budget_total,
            )
            return HookDecision(decision="pass")
        try:
            response = await _invoke_delegate(config, rendered.task_text)
        except TimeoutError:
            _log.warning(
                "agent hook (%s): subagent timed out after %.1fs; passing",
                config.event,
                config.timeout_seconds,
            )
            return HookDecision(decision="pass")
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "agent hook (%s): subagent dispatch failed (%s); passing",
                config.event,
                exc,
            )
            return HookDecision(decision="pass")
        if config.returns == "structured":
            return _parse_response_structured(response)
        return _parse_response_allow_block(response)

    return _handler


__all__ = [
    "make_agent_hook_handler",
]

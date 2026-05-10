"""LLM-prompt hook handler factory (v1.1 plan-2 M8.1, 2026-05-09).

Mirrors :mod:`opencomputer.hooks.shell_handlers` for prompt-style hooks.
Users declare a hook with ``type: prompt`` in ``config.yaml``; this
module wraps each :class:`HookPromptConfig` in an async handler that:

1. Renders the :class:`HookContext` into a user message describing the
   tool call / event payload.
2. Calls the auxiliary LLM (:func:`opencomputer.agent.aux_llm.complete_text`)
   with a strict timeout + token cap.
3. Parses the response into a :class:`HookDecision`:
   - ``returns: allow_block`` (default) — looks for "block" / "allow" in
     the first non-empty line; "block: <reason>" carries the reason.
   - ``returns: score`` — extracts a numeric risk score; >= threshold blocks.

Fail-open contract (matches the shell-hook contract):
- LLM call raises → log WARN + ``decision="pass"``.
- Timeout exceeded → log WARN + ``decision="pass"``.
- Estimated tokens > ``token_budget_input`` → refuse to call + log + pass.
- Unparseable response → log + pass.

The whole point: a wedged or expensive aux-LLM must never wedge or
bankrupt the agent loop. Risk-rating hooks are advisory by construction.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass

from opencomputer.agent.config import HookPromptConfig
from plugin_sdk.hooks import HookContext, HookDecision, HookEvent, HookHandler

_log = logging.getLogger("opencomputer.hooks.prompt")

# Approximate tokens-per-character ratio for English text. Used to
# estimate input size before calling the LLM. 4 is the rule-of-thumb
# Anthropic / OpenAI recommend; tighter would over-reject, looser would
# defeat the cap.
_CHARS_PER_TOKEN: int = 4


@dataclass(frozen=True, slots=True)
class _RenderedMessage:
    """Aux-LLM request inputs derived from a HookContext."""

    user_message: str
    estimated_input_tokens: int


def _render_context(
    config: HookPromptConfig, ctx: HookContext
) -> _RenderedMessage:
    """Convert a HookContext into a string the aux-LLM can reason about.

    Includes the event name, tool name (if any), tool args (truncated),
    matcher (if set on the config), and a hint at what return shape is
    expected. The system prompt is the user's policy; the rendered
    HookContext is the user message.
    """
    parts: list[str] = []
    parts.append(f"Hook event: {ctx.event.value if isinstance(ctx.event, HookEvent) else ctx.event}")
    if ctx.tool_call is not None:
        if ctx.tool_call.name:
            parts.append(f"Tool: {ctx.tool_call.name}")
        if config.matcher:
            parts.append(f"Matcher: {config.matcher}")
        if ctx.tool_call.arguments:
            # Truncate args to keep the budget low. JSON is the most
            # compact + readable form for the LLM.
            try:
                args_json = json.dumps(
                    ctx.tool_call.arguments, default=str, indent=None
                )
            except (TypeError, ValueError):
                args_json = repr(ctx.tool_call.arguments)
            if len(args_json) > 800:
                args_json = args_json[:800] + "...[truncated]"
            parts.append(f"Args: {args_json}")
    elif config.matcher:
        # Matcher set but no tool_call — still useful for policy context.
        parts.append(f"Matcher: {config.matcher}")
    if ctx.session_id:
        parts.append(f"Session: {ctx.session_id}")
    user_msg = "\n".join(parts)
    estimated = max(1, len(user_msg) // _CHARS_PER_TOKEN)
    return _RenderedMessage(user_message=user_msg, estimated_input_tokens=estimated)


_BLOCK_PATTERN = re.compile(r"^\s*block(?:\s*[:.\-]\s*(.+))?$", re.IGNORECASE)
_ALLOW_PATTERN = re.compile(r"^\s*(?:allow|approve|pass|ok)\s*$", re.IGNORECASE)
_SCORE_PATTERN = re.compile(r"\b(\d+(?:\.\d+)?)\b")


def _parse_response_allow_block(text: str) -> HookDecision:
    """Parse an LLM response under ``returns: allow_block`` semantics.

    Looks at the first non-empty line for ``block: <reason>`` /
    ``block`` / ``allow``. Anything ambiguous → pass (fail-open).
    """
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        m = _BLOCK_PATTERN.match(stripped)
        if m is not None:
            reason = (m.group(1) or "blocked by prompt hook").strip()
            return HookDecision(decision="block", reason=reason)
        if _ALLOW_PATTERN.match(stripped):
            return HookDecision(decision="pass")
        # First substantive line that's neither — fail-open.
        _log.debug(
            "prompt hook: ambiguous LLM response %r; passing", stripped[:120]
        )
        return HookDecision(decision="pass")
    return HookDecision(decision="pass")


def _parse_response_score(
    text: str, threshold: float
) -> HookDecision:
    """Parse an LLM response under ``returns: score`` semantics.

    Pulls the first numeric token; if >= threshold, returns block.
    No numeric → fail-open.
    """
    m = _SCORE_PATTERN.search(text)
    if m is None:
        _log.debug(
            "prompt hook: no score in response %r; passing", text[:120]
        )
        return HookDecision(decision="pass")
    try:
        score = float(m.group(1))
    except ValueError:
        return HookDecision(decision="pass")
    if score >= threshold:
        return HookDecision(
            decision="block",
            reason=f"risk score {score} >= {threshold}",
        )
    return HookDecision(decision="pass")


async def _invoke_llm(
    config: HookPromptConfig, user_message: str
) -> str:
    """Call the aux LLM with the configured timeout + output cap.

    Returns the assistant text. Raises any underlying SDK error so the
    handler can convert it into a fail-open + warning log.
    """
    # Lazy import: aux_llm pulls in the whole provider registry, which
    # we don't want to drag into module-load time.
    from opencomputer.agent.aux_llm import complete_text  # noqa: PLC0415

    model: str | None = None if config.model in ("", "auto") else config.model
    return await asyncio.wait_for(
        complete_text(
            messages=[{"role": "user", "content": user_message}],
            system=config.system,
            max_tokens=config.token_budget_output,
            model=model,
        ),
        timeout=config.timeout_seconds,
    )


def make_prompt_hook_handler(config: HookPromptConfig) -> HookHandler:
    """Wrap a :class:`HookPromptConfig` in an async :class:`HookHandler`.

    The returned handler is suitable for registration via
    :class:`HookSpec`. Mirrors :func:`make_shell_hook_handler` in
    structure: render-and-invoke under one ``try`` block, broad
    fail-open on every error path.
    """

    async def _handler(ctx: HookContext) -> HookDecision:
        rendered = _render_context(config, ctx)
        if rendered.estimated_input_tokens > config.token_budget_input:
            _log.warning(
                "prompt hook (%s): estimated input %d tokens > budget %d; "
                "passing without LLM call",
                config.event,
                rendered.estimated_input_tokens,
                config.token_budget_input,
            )
            return HookDecision(decision="pass")
        try:
            response = await _invoke_llm(config, rendered.user_message)
        except TimeoutError:
            _log.warning(
                "prompt hook (%s): aux LLM timed out after %.1fs; passing",
                config.event,
                config.timeout_seconds,
            )
            return HookDecision(decision="pass")
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "prompt hook (%s): aux LLM call failed (%s); passing",
                config.event,
                exc,
            )
            return HookDecision(decision="pass")
        if config.returns == "score":
            return _parse_response_score(response, config.score_threshold)
        return _parse_response_allow_block(response)

    return _handler


__all__ = [
    "make_prompt_hook_handler",
]

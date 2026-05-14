"""hookify — auto-loading rule engine for OC hooks.

Reads ``.md`` rule files from ``$OPENCOMPUTER_PROFILE_HOME/hookify/``
(and optionally ``<cwd>/.opencomputer/hookify/``) and registers them
as one of four OC hook handlers covering the Anthropic hookify
event-family map:

    bash    → PreToolUse on Bash
    file    → PreToolUse on Edit|Write|MultiEdit
    all     → PreToolUse on any tool
    post    → PostToolUse on any tool
    stop    → Stop
    prompt  → UserPromptSubmit

Rules are loaded *every* hook invocation (not cached at register
time) so editing a rule file and saving takes effect immediately —
no restart needed.
"""

from __future__ import annotations

from rule_engine import RuleEngine  # type: ignore[import-not-found]
from rule_loader import load_rules  # type: ignore[import-not-found]

from plugin_sdk.hooks import HookContext, HookDecision, HookEvent, HookSpec

_engine = RuleEngine()


def _make_handler(family: str):
    """Build a hook handler that filters to ``family`` rules."""

    async def handler(ctx: HookContext) -> HookDecision:
        rules = load_rules(event=family)
        if not rules:
            return HookDecision(decision="pass")
        tool_name = (ctx.tool_call.name if ctx.tool_call else "") or ""
        tool_input = (
            ctx.tool_call.arguments if ctx.tool_call else None
        )
        extra: dict = {}
        # Stop / UserPromptSubmit don't carry tool_call; pull the
        # matching field from ctx if it's available.
        if family == "prompt" and ctx.message is not None:
            extra["user_prompt"] = ctx.message.content
        return _engine.evaluate(
            rules,
            tool_name=tool_name,
            tool_input=tool_input,
            extra=extra,
        )

    return handler


def register(api) -> None:  # noqa: D401 — duck-typed PluginAPI
    """Register one HookSpec per Anthropic-hookify event family."""
    api.register_hook(
        HookSpec(
            event=HookEvent.PRE_TOOL_USE,
            handler=_make_handler("bash"),
            matcher=r"Bash",
            fire_and_forget=False,
            timeout_ms=2000,
        )
    )
    api.register_hook(
        HookSpec(
            event=HookEvent.PRE_TOOL_USE,
            handler=_make_handler("file"),
            matcher=r"Edit|Write|MultiEdit",
            fire_and_forget=False,
            timeout_ms=2000,
        )
    )
    api.register_hook(
        HookSpec(
            event=HookEvent.PRE_TOOL_USE,
            handler=_make_handler("all"),
            matcher=r".*",
            fire_and_forget=False,
            timeout_ms=2000,
        )
    )
    api.register_hook(
        HookSpec(
            event=HookEvent.POST_TOOL_USE,
            handler=_make_handler("post"),
            matcher=r".*",
            fire_and_forget=True,
            timeout_ms=2000,
        )
    )
    api.register_hook(
        HookSpec(
            event=HookEvent.STOP,
            handler=_make_handler("stop"),
            fire_and_forget=False,
            timeout_ms=2000,
        )
    )
    api.register_hook(
        HookSpec(
            event=HookEvent.USER_PROMPT_SUBMIT,
            handler=_make_handler("prompt"),
            fire_and_forget=False,
            timeout_ms=2000,
        )
    )

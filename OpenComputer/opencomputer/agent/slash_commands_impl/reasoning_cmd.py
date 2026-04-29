"""``/reasoning [level|show|hide|status]`` — control thinking display + effort.

Tier 2.A.8 from docs/refs/hermes-agent/2026-04-28-major-gaps.md.

Two orthogonal knobs:

1. **Effort level** (sets ``runtime.custom["reasoning_effort"]``):
   ``none``, ``minimal``, ``low``, ``medium``, ``high``, ``xhigh``.
   Provider plugins read this to set provider-specific reasoning params
   (e.g. Anthropic's ``thinking.budget_tokens``, OpenAI ``o1`` series'
   ``reasoning_effort`` field).

2. **Display toggle** (sets ``runtime.custom["show_reasoning"]``):
   ``show`` reveals ``<think>`` blocks in streamed output;
   ``hide`` strips them. Default: hidden.

Examples:
    /reasoning              → status (current effort + display state)
    /reasoning high         → set effort to high
    /reasoning show         → show <think> blocks
    /reasoning hide         → hide <think> blocks
    /reasoning none         → disable reasoning entirely
    /reasoning status       → explicit status (same as no args)
"""

from __future__ import annotations

from plugin_sdk.runtime_context import RuntimeContext
from plugin_sdk.slash_command import SlashCommand, SlashCommandResult

_VALID_LEVELS: tuple[str, ...] = (
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
)
_DEFAULT_LEVEL = "medium"

_USAGE = (
    "Usage: /reasoning [level|show|hide|status]\n"
    "  Levels: none, minimal, low, medium, high, xhigh\n"
    "  show / hide: toggle <think> block display\n"
    "  status (or no arg): show current settings"
)


def _current_level(runtime: RuntimeContext) -> str:
    return str(runtime.custom.get("reasoning_effort", _DEFAULT_LEVEL))


def _current_show(runtime: RuntimeContext) -> bool:
    return bool(runtime.custom.get("show_reasoning", False))


def _format_status(runtime: RuntimeContext) -> str:
    level = _current_level(runtime)
    show = "shown" if _current_show(runtime) else "hidden"
    return f"reasoning: effort={level}, display={show}"


class ReasoningCommand(SlashCommand):
    name = "reasoning"
    description = "Control reasoning effort + thinking-block display"

    async def execute(self, args: str, runtime: RuntimeContext) -> SlashCommandResult:
        sub = (args or "").strip().lower()

        if sub in ("", "status"):
            return SlashCommandResult(output=_format_status(runtime), handled=True)

        if sub == "show":
            runtime.custom["show_reasoning"] = True
            return SlashCommandResult(
                output=f"<think> blocks now SHOWN. {_format_status(runtime)}",
                handled=True,
            )

        if sub == "hide":
            runtime.custom["show_reasoning"] = False
            return SlashCommandResult(
                output=f"<think> blocks now HIDDEN. {_format_status(runtime)}",
                handled=True,
            )

        if sub in _VALID_LEVELS:
            runtime.custom["reasoning_effort"] = sub
            return SlashCommandResult(
                output=f"reasoning effort set to {sub}. {_format_status(runtime)}",
                handled=True,
            )

        return SlashCommandResult(output=_USAGE, handled=True)


__all__ = ["ReasoningCommand"]

"""``/reasoning [level|show [N|all|last]|hide|status]`` — control thinking display + effort.

Three orthogonal knobs:

1. **Effort level** (sets ``runtime.custom["reasoning_effort"]``):
   ``none``, ``minimal``, ``low``, ``medium``, ``high``, ``xhigh``.
   Provider plugins read this to set provider-specific reasoning params
   (e.g. Anthropic's ``thinking.budget_tokens``, OpenAI ``o1`` series'
   ``reasoning_effort`` field).

2. **Display toggle for FUTURE turns** (sets ``runtime.custom["show_reasoning"]``):
   ``show`` reveals streamed ``<think>`` blocks; ``hide`` strips them.
   Default: hidden.

3. **Retroactive expand of PAST turns** (reads ``runtime.custom["_reasoning_store"]``):
   ``show`` (or ``show last``) prints the most recent turn as a tree.
   ``show <N>`` prints turn N. ``show all`` prints every turn in the store.

Examples::

    /reasoning              → status
    /reasoning high         → set effort to high
    /reasoning show         → expand the LAST turn AND show <think> on next turns
    /reasoning show 5       → expand turn #5 only
    /reasoning show all     → expand every turn in the store
    /reasoning hide         → hide <think> on future turns
    /reasoning none         → disable reasoning entirely
"""

from __future__ import annotations

import re

from opencomputer.cli_ui.reasoning_store import ReasoningStore, render_turns_to_text
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
    "Usage: /reasoning [level|show [N|all|last]|hide|status]\n"
    "  Levels: none, minimal, low, medium, high, xhigh\n"
    "  show          → expand the last turn (and show <think> next turns)\n"
    "  show <N>      → expand turn #N\n"
    "  show all      → expand every turn in the store\n"
    "  hide          → hide <think> on future turns\n"
    "  status        → show current settings"
)


def _current_level(runtime: RuntimeContext) -> str:
    return str(runtime.custom.get("reasoning_effort", _DEFAULT_LEVEL))


def _current_show(runtime: RuntimeContext) -> bool:
    return bool(runtime.custom.get("show_reasoning", False))


def _format_status(runtime: RuntimeContext) -> str:
    level = _current_level(runtime)
    show = "shown" if _current_show(runtime) else "hidden"
    return f"reasoning: effort={level}, display={show}"


def _get_store(runtime: RuntimeContext) -> ReasoningStore | None:
    candidate = runtime.custom.get("_reasoning_store")
    return candidate if isinstance(candidate, ReasoningStore) else None


_SHOW_ID_PATTERN = re.compile(r"^show\s+(\d+)$")


class ReasoningCommand(SlashCommand):
    name = "reasoning"
    description = (
        "Show past reasoning + control reasoning effort + thinking-block display"
    )

    async def execute(self, args: str, runtime: RuntimeContext) -> SlashCommandResult:
        sub = (args or "").strip().lower()

        if sub in ("", "status"):
            return SlashCommandResult(output=_format_status(runtime), handled=True)

        # --- show variants ---------------------------------------------------
        if sub in ("show", "show last"):
            # Also flip the future-turns flag so streaming providers
            # expose raw <think> on the NEXT turn — preserves the
            # original /reasoning show contract.
            runtime.custom["show_reasoning"] = True
            store = _get_store(runtime)
            if store is None:
                return SlashCommandResult(
                    output=(
                        "no reasoning history available "
                        "(store not attached to this session). "
                        f"{_format_status(runtime)}"
                    ),
                    handled=True,
                )
            turn = store.get_latest()
            if turn is None:
                return SlashCommandResult(
                    output="no reasoning turns recorded yet.",
                    handled=True,
                )
            return SlashCommandResult(
                output=render_turns_to_text([turn]), handled=True
            )

        if sub == "show all":
            store = _get_store(runtime)
            if store is None:
                return SlashCommandResult(
                    output=(
                        "no reasoning history available "
                        "(store not attached to this session)."
                    ),
                    handled=True,
                )
            turns = store.get_all()
            if not turns:
                return SlashCommandResult(
                    output="no reasoning turns recorded yet.",
                    handled=True,
                )
            return SlashCommandResult(
                output=render_turns_to_text(turns), handled=True
            )

        m = _SHOW_ID_PATTERN.match(sub)
        if m:
            store = _get_store(runtime)
            if store is None:
                return SlashCommandResult(
                    output=(
                        "no reasoning history available "
                        "(store not attached to this session)."
                    ),
                    handled=True,
                )
            turn_id = int(m.group(1))
            turn = store.get_by_id(turn_id)
            if turn is None:
                known = [t.turn_id for t in store.get_all()]
                known_str = str(known) if known else "none"
                return SlashCommandResult(
                    output=(
                        f"no turn #{turn_id} in store "
                        f"(known turns: {known_str})."
                    ),
                    handled=True,
                )
            return SlashCommandResult(
                output=render_turns_to_text([turn]), handled=True
            )

        # --- legacy hide / level setters -------------------------------------
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

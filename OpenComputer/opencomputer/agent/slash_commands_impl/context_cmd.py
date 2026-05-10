"""``/context`` — show context-window usage + compaction count.

CC §4 + §10 visibility surface (closes the gap documented in
``docs/OC-FROM-CLAUDE-CODE.md``). Reads from ``runtime.custom`` keys the
agent loop populates each turn:

  - ``model``               — current model id (loop sets this each turn)
  - ``session_tokens_in``   — cumulative input tokens this session
  - ``last_input_tokens``   — current-turn input tokens (preferred for %)
  - ``session_compactions`` — compaction count (loop bumps after each
                              ``CompactionResult.did_compact``)

Output renders a markdown panel (same shape as ``/usage``).

Spec:
    docs/superpowers/specs/2026-05-10-cc-usage-context-visibility-design.md
"""

from __future__ import annotations

import logging

from plugin_sdk.runtime_context import RuntimeContext
from plugin_sdk.slash_command import SlashCommand, SlashCommandResult

_LOG = logging.getLogger(__name__)

#: Compaction trigger threshold. OC's CompactionEngine triggers at 98%
#: of the resolved context window — see compaction.should_compact.
_COMPACTION_TRIGGER_PCT: float = 0.98


def _coerce_int(value: object, default: int = 0) -> int:
    """Best-effort int coercion. Adversarial inputs (strings, None,
    float NaN, list) fall back to ``default`` rather than raising —
    a buggy plugin must not crash ``/context``."""
    if value is None:
        return default
    if isinstance(value, bool):  # bool is int-subclass; pre-empt that
        return int(value)
    if isinstance(value, (int, float)):
        try:
            return int(value)
        except (ValueError, OverflowError):
            return default
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


class ContextCommand(SlashCommand):
    """``/context`` — context window % used + compaction count."""

    name = "context"
    description = "Show context window usage + compaction count this session"

    async def execute(self, args: str, runtime: RuntimeContext) -> SlashCommandResult:
        custom = runtime.custom if runtime is not None else {}

        from opencomputer.agent.compaction import resolve_window_safe

        model_raw = custom.get("model") or ""
        model = str(model_raw) if model_raw else ""
        max_ctx = resolve_window_safe(model)

        # Prefer current-turn (``last_input_tokens``) over cumulative
        # session count for the "% used" calculation. The cumulative
        # value double-counts pre-compaction tokens after a rewrite.
        last_input = _coerce_int(custom.get("last_input_tokens"), 0)
        session_in = _coerce_int(custom.get("session_tokens_in"), 0)
        used = last_input if last_input > 0 else session_in

        compactions = _coerce_int(custom.get("session_compactions"), 0)

        pct = (used / max_ctx * 100.0) if max_ctx > 0 else 0.0
        remaining = max_ctx - used

        lines = ["## Context window"]
        lines.append(f"  model: {model or '(unknown)'}")
        lines.append(f"  used: {used:,} / {max_ctx:,} ({pct:.1f}%)")
        lines.append(f"  remaining: {remaining:,} tokens")
        lines.append(
            f"  compaction triggers at: {_COMPACTION_TRIGGER_PCT * 100:.0f}%"
        )
        lines.append(f"  compactions this session: {compactions}")
        lines.append(f"  total session input tokens: {session_in:,}")

        return SlashCommandResult(output="\n".join(lines), handled=True)


__all__ = ["ContextCommand"]

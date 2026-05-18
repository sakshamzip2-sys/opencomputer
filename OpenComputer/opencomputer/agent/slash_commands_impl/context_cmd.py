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
    # gateway_safe: Dispatch._populate_session_usage fills
    # session_tokens_in / session_compactions / model into the runtime
    # from SessionDB.session_usage_summary, so /context shows real data
    # on the gateway, not a placeholder 0%.
    gateway_safe = True

    async def execute(self, args: str, runtime: RuntimeContext) -> SlashCommandResult:
        custom = runtime.custom if runtime is not None else {}

        # 2026-05-11: use the single shared resolvers from
        # ``compaction`` so this command and the TUI status-line bar
        # render the same numbers. Before this layer, both surfaces
        # hand-typed their own logic and drifted (bar double-counted
        # cumulative in+out; this command displayed "98%" for a
        # threshold the engine fires at 80%).
        from opencomputer.agent.compaction import (
            resolve_current_input_tokens,
            resolve_effective_compaction_threshold_ratio,
            resolve_window_safe,
        )

        model_raw = custom.get("model") or ""
        model = str(model_raw) if model_raw else ""
        max_ctx = resolve_window_safe(model)

        used = resolve_current_input_tokens(custom)
        session_in = _coerce_int(custom.get("session_tokens_in"), 0)
        compactions = _coerce_int(custom.get("session_compactions"), 0)
        trigger_ratio = resolve_effective_compaction_threshold_ratio(custom)

        pct = (used / max_ctx * 100.0) if max_ctx > 0 else 0.0
        remaining = max_ctx - used

        lines = ["## Context window"]
        lines.append(f"  model: {model or '(unknown)'}")
        lines.append(f"  used: {used:,} / {max_ctx:,} ({pct:.1f}%)")
        lines.append(f"  remaining: {remaining:,} tokens")
        lines.append(
            f"  compaction triggers at: {trigger_ratio * 100:.0f}%"
        )
        lines.append(f"  compactions this session: {compactions}")
        lines.append(f"  total session input tokens: {session_in:,}")

        return SlashCommandResult(output="\n".join(lines), handled=True)


__all__ = ["ContextCommand"]

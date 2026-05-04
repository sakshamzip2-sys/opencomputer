"""Tool-call loop guardrails.

Detects identical tool-name+args repetition within a turn and either warns
(at ``warn_at`` consecutive identical calls) or hard-stops (at ``stop_at``).

Mirrors hermes-agent ``58b89965c`` + ``0704589ce`` (warning-first refactor),
adapted to OC's flat agent layout.

Wired into :func:`opencomputer.agent.loop.AgentLoop._dispatch_tool_calls`:
the guard ``observe()`` is called per tool call before consent + PreToolUse
hooks; on a ``warn`` verdict the message is logged; on ``stop`` the guard
raises :exc:`ToolLoopGuardrailError` which the loop catches and converts
to a turn-end with the user-visible reason.

NOTE (Wave 5 dedupe): OC also has :class:`opencomputer.agent.loop_safety.LoopDetector`
which uses a sliding-window approach. The two coexist: ``LoopDetector``
catches near-repetitions across recent calls; ``ToolLoopGuard`` catches
deterministic tight loops with identical tool+args (where the streak
counter trips faster). Either may fire first; both are non-fatal except
at their respective hard-stop thresholds.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

DEFAULT_WARN_AT: int = 10
DEFAULT_STOP_AT: int = 25


class ToolLoopGuardrailError(RuntimeError):
    """Raised when the guard's stop threshold is hit."""


@dataclass(slots=True, frozen=True)
class GuardrailVerdict:
    """Outcome of a single :meth:`ToolLoopGuard.observe` call."""

    level: Literal["ok", "warn"]
    message: str = ""


class ToolLoopGuard:
    """Detect identical-tool-call repetition within a turn.

    The guard maintains a single 'last canonical key' + streak counter.
    A different tool name OR different arguments resets the streak.
    """

    def __init__(
        self,
        *,
        warn_at: int = DEFAULT_WARN_AT,
        stop_at: int = DEFAULT_STOP_AT,
        enabled: bool = True,
    ) -> None:
        if warn_at < 1:
            raise ValueError("warn_at must be >= 1")
        if stop_at < warn_at:
            raise ValueError("stop_at must be >= warn_at")
        self._warn_at = warn_at
        self._stop_at = stop_at
        self._enabled = enabled
        self._last_key: str | None = None
        self._streak: int = 0

    def reset(self) -> None:
        """Clear the streak — call at the start of each user turn."""
        self._last_key = None
        self._streak = 0

    def observe(self, tool_call: dict[str, Any]) -> GuardrailVerdict:
        """Record a tool call attempt and return the resulting verdict.

        Raises :class:`ToolLoopGuardrailError` when the stop threshold is hit
        (after the streak counter is incremented).
        """
        if not self._enabled:
            return GuardrailVerdict(level="ok")

        key = self._key(tool_call)
        if key == self._last_key:
            self._streak += 1
        else:
            self._last_key = key
            self._streak = 1

        name = tool_call.get("name", "?")

        if self._streak >= self._stop_at:
            raise ToolLoopGuardrailError(
                f"Tool-loop guardrail: '{name}' repeated "
                f"{self._streak} consecutive calls (stop_at={self._stop_at})."
            )

        if self._streak == self._warn_at:
            return GuardrailVerdict(
                level="warn",
                message=(
                    f"Tool-loop guardrail: '{name}' has run "
                    f"{self._streak} consecutive identical calls "
                    f"(warn_at={self._warn_at}, stop_at={self._stop_at})."
                ),
            )
        return GuardrailVerdict(level="ok")

    @staticmethod
    def _key(tool_call: dict[str, Any]) -> str:
        name = tool_call.get("name", "")
        args = tool_call.get("arguments") or {}
        return f"{name}|{json.dumps(args, sort_keys=True, default=str)}"

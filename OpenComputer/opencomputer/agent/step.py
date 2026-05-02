"""
StepOutcome — clean dataclass for one iteration of the agent loop.

Inspired by kimi-cli's KimiSoul.Step pattern. Makes the loop testable:
every iteration returns a StepOutcome the caller can assert against.
"""

from __future__ import annotations

from dataclasses import dataclass

from plugin_sdk.core import Message, StopReason


@dataclass(frozen=True, slots=True)
class StepOutcome:
    """Result of one iteration of the agent loop."""

    stop_reason: StopReason
    assistant_message: Message
    tool_calls_made: int = 0  # number of tools invoked this iteration
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def should_continue(self) -> bool:
        """Whether the loop should keep iterating."""
        return self.stop_reason == StopReason.TOOL_USE


__all__ = ["StepOutcome"]

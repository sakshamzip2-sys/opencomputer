"""Smoke tests for new StopReason enum members.

StopReason lives in plugin_sdk (public contract) so adding REFUSAL +
CONTEXT_FULL is a public-API change. BC rule #4 in
plugin_sdk/CLAUDE.md: additive only, no removals.
"""

from __future__ import annotations

from plugin_sdk import StopReason


def test_refusal_member_exists() -> None:
    assert StopReason.REFUSAL.value == "refusal"


def test_context_full_member_exists() -> None:
    assert StopReason.CONTEXT_FULL.value == "context_full"


def test_existing_members_unchanged() -> None:
    """BC: existing values must remain stable string-equal."""
    assert StopReason.END_TURN.value == "end_turn"
    assert StopReason.TOOL_USE.value == "tool_use"
    assert StopReason.MAX_TOKENS.value == "max_tokens"
    assert StopReason.INTERRUPTED.value == "interrupted"
    assert StopReason.BUDGET_EXHAUSTED.value == "budget_exhausted"
    assert StopReason.ERROR.value == "error"

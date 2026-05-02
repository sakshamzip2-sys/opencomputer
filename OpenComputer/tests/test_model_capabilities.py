"""Tests for opencomputer.agent.model_capabilities — pure functions, no I/O."""

from __future__ import annotations

import pytest

from opencomputer.agent.model_capabilities import (
    supports_adaptive_thinking,
    supports_temperature,
    thinking_display_default,
)


@pytest.mark.parametrize("model,expected", [
    # Adaptive-required (Opus 4.7 forward + Mythos)
    ("claude-opus-4-7", True),
    ("claude-opus-4-7-20260301", True),
    ("claude-mythos-2026-preview", True),
    ("claude-opus-4-8-future", True),
    # Adaptive-recommended (4.6)
    ("claude-opus-4-6", True),
    ("claude-sonnet-4-6", True),
    ("claude-sonnet-4-6-20251101", True),
    # Legacy-thinking-only (4.5 and older)
    ("claude-opus-4-5", False),
    ("claude-sonnet-4-5", False),
    ("claude-haiku-4-5-20251001", False),
    ("claude-sonnet-3-7-20250219", False),
    ("claude-haiku-3-20240307", False),
    # Forward-default for unknown claude-* (modern assumption)
    ("claude-future-x", True),
    # Non-claude (no thinking concept here)
    ("gpt-4o", False),
    ("o1-preview", False),
    ("llama-3-70b", False),
])
def test_supports_adaptive_thinking(model: str, expected: bool) -> None:
    assert supports_adaptive_thinking(model) is expected


@pytest.mark.parametrize("model,expected", [
    # Opus 4.7+ and Mythos: temperature removed
    ("claude-opus-4-7", False),
    ("claude-mythos-2026-preview", False),
    ("claude-opus-4-8-future", False),
    # 4.6 and older still accept temperature
    ("claude-opus-4-6", True),
    ("claude-sonnet-4-6", True),
    ("claude-opus-4-5", True),
    ("claude-haiku-4-5", True),
    ("claude-sonnet-3-7", True),
    # Forward-default for unknown claude-*: assume modern (no temperature)
    ("claude-future-x", False),
    # Non-claude unaffected
    ("gpt-4o", True),
    ("o1-preview", True),
])
def test_supports_temperature(model: str, expected: bool) -> None:
    assert supports_temperature(model) is expected


@pytest.mark.parametrize("model,expected", [
    ("claude-opus-4-7", "summarized"),
    ("claude-mythos-2026-preview", "summarized"),
    ("claude-opus-4-6", "summarized"),
    ("claude-sonnet-4-6", "summarized"),
    # Legacy models don't use the display field — function returns "" so
    # callers can skip the kwarg entirely.
    ("claude-opus-4-5", ""),
    ("claude-haiku-4-5", ""),
    ("gpt-4o", ""),
])
def test_thinking_display_default(model: str, expected: str) -> None:
    assert thinking_display_default(model) == expected

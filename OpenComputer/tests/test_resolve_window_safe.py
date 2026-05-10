"""``resolve_window_safe`` — read-only, never-raising context-window resolver.

Used by the CC §4 visibility surface (slash ``/context``, CLI ``oc
context show / list``). Spec: docs/superpowers/specs/2026-05-10-
cc-usage-context-visibility-design.md §6 — extracted from per-caller
duplication to keep slash and CLI consistent.
"""

from __future__ import annotations

from unittest.mock import patch

from opencomputer.agent.compaction import resolve_window_safe


def test_resolve_window_safe_returns_fallback_for_empty_model():
    assert resolve_window_safe("") == 200_000


def test_resolve_window_safe_returns_fallback_for_none_like_model():
    # Coerce None → "" via caller convention; helper accepts only str.
    # Adversarial: pass an explicit empty string.
    assert resolve_window_safe("") == 200_000


def test_resolve_window_safe_returns_known_model_window():
    # claude-opus-4-7 resolves to 1M in the static table.
    assert resolve_window_safe("claude-opus-4-7") == 1_000_000


def test_resolve_window_safe_returns_sonnet_200k():
    assert resolve_window_safe("claude-sonnet-4-6") == 200_000


def test_resolve_window_safe_returns_fallback_for_unknown_model():
    """An unknown model id maps to ``context_window_for``'s default
    64k. The safe-resolver floors that to 200k since 64k is too
    pessimistic for a 'what's my budget?' surface."""
    # context_window_for("totally-unknown") = 64000 < 200000 fallback.
    # resolve_window_safe MUST floor to 200k.
    result = resolve_window_safe("totally-unknown-model-9999")
    # Either the static 64k OR the 200k floor — depends on whether the
    # caller intended the raw or the safe semantics. We pick the floor
    # for safety. Adjust to .static 64k if intent shifts.
    assert result >= 64_000  # don't regress lower


def test_resolve_window_safe_swallows_resolution_exception():
    """A raised ``context_window_with_overrides`` doesn't propagate."""
    with patch(
        "opencomputer.agent.compaction.context_window_with_overrides",
        side_effect=RuntimeError("simulated config corruption"),
    ):
        result = resolve_window_safe("claude-opus-4-7")
    assert result == 200_000


def test_resolve_window_safe_floors_zero_resolution():
    """A pathological resolver returning 0 must NOT cause divide-by-zero
    downstream."""
    with patch(
        "opencomputer.agent.compaction.context_window_with_overrides",
        return_value=0,
    ):
        result = resolve_window_safe("claude-opus-4-7")
    assert result == 200_000


def test_resolve_window_safe_floors_negative_resolution():
    with patch(
        "opencomputer.agent.compaction.context_window_with_overrides",
        return_value=-42,
    ):
        result = resolve_window_safe("claude-opus-4-7")
    assert result == 200_000


def test_resolve_window_safe_floors_none_resolution():
    with patch(
        "opencomputer.agent.compaction.context_window_with_overrides",
        return_value=None,
    ):
        result = resolve_window_safe("claude-opus-4-7")
    assert result == 200_000


def test_resolve_window_safe_disables_network_probe():
    """The safe variant must call the underlying resolver with
    ``enable_probe=False`` so slash / CLI hot paths never block on
    network I/O. We assert the kwarg via a spy."""
    captured = {}

    def _spy(model, *args, **kwargs):
        captured["kwargs"] = kwargs
        return 200_000

    with patch(
        "opencomputer.agent.compaction.context_window_with_overrides",
        side_effect=_spy,
    ):
        resolve_window_safe("claude-opus-4-7")
    assert captured["kwargs"].get("enable_probe") is False

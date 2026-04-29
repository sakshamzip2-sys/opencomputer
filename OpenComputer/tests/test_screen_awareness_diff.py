"""Tests for compute_screen_delta() — line-level diff between pre and
post OCR text. Used to attach a `_screen_delta` field to tool results
so the agent can see what visibly changed."""
from __future__ import annotations

from extensions.screen_awareness.diff import ScreenDelta, compute_screen_delta


def test_diff_identical_screens_yields_no_changes():
    pre = "Login\nEmail\nPassword"
    post = "Login\nEmail\nPassword"
    delta = compute_screen_delta(pre, post)
    assert delta.added == ()
    assert delta.removed == ()


def test_diff_added_lines_only():
    pre = "Login\nEmail"
    post = "Login\nEmail\nPassword\nSign In"
    delta = compute_screen_delta(pre, post)
    assert delta.added == ("Password", "Sign In")
    assert delta.removed == ()


def test_diff_removed_lines_only():
    pre = "Login\nEmail\nPassword"
    post = "Welcome"
    delta = compute_screen_delta(pre, post)
    assert "Welcome" in delta.added
    assert "Login" in delta.removed
    assert "Email" in delta.removed
    assert "Password" in delta.removed


def test_diff_empty_pre_treats_all_as_added():
    pre = ""
    post = "Hello\nWorld"
    delta = compute_screen_delta(pre, post)
    assert delta.added == ("Hello", "World")
    assert delta.removed == ()


def test_diff_empty_post_treats_all_as_removed():
    pre = "Hello\nWorld"
    post = ""
    delta = compute_screen_delta(pre, post)
    assert delta.added == ()
    assert delta.removed == ("Hello", "World")


def test_diff_normalizes_whitespace_lines():
    """Lines that differ only in leading/trailing whitespace are NOT
    treated as different — OCR jitter shouldn't show as a change."""
    pre = "  Login  \nEmail\n   "
    post = "Login\nEmail"
    delta = compute_screen_delta(pre, post)
    # Empty/whitespace-only line in `pre` is dropped in normalization
    assert delta.added == ()
    assert delta.removed == ()


def test_diff_returns_immutable_tuples():
    """Returned added/removed are tuples (frozen). Callers can't mutate."""
    delta = compute_screen_delta("a", "b")
    assert isinstance(delta.added, tuple)
    assert isinstance(delta.removed, tuple)

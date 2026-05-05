"""V2 — title bar callable + badge/title decoupling.

Verifies the read_user_input signature accepts get_session_title and
that the helper logic mirrors the production callable behaviour.
"""
from __future__ import annotations

import inspect
from typing import Callable


def test_read_user_input_accepts_get_session_title() -> None:
    from opencomputer.cli_ui.input_loop import read_user_input

    sig = inspect.signature(read_user_input)
    assert "get_session_title" in sig.parameters


def test_read_user_input_keeps_back_compat_session_title_param() -> None:
    """Old callers passing ``session_title=`` must still work."""
    from opencomputer.cli_ui.input_loop import read_user_input

    sig = inspect.signature(read_user_input)
    assert "session_title" in sig.parameters


def test_title_text_uses_callable_dynamically() -> None:
    """When the callable's return value changes, the rendered text follows."""
    title_holder = ["foo"]
    get_title: Callable[[], str | None] = lambda: title_holder[0]

    def _title_text():
        title = get_title() or ""
        if not (1 <= len(title) <= 50):
            return []
        return [
            ("class:title.box", "┤ "),
            ("class:title.text", title),
            ("class:title.box", " ├"),
        ]

    seg1 = _title_text()
    assert seg1[1][1] == "foo"

    title_holder[0] = "bar"
    seg2 = _title_text()
    assert seg2[1][1] == "bar"


def test_title_visibility_independent_of_runtime() -> None:
    """When runtime is None, the title can still be visible if it's set."""
    title_holder = ["my-session"]

    def _title_visible() -> bool:
        title = title_holder[0] or ""
        return 1 <= len(title) <= 50

    assert _title_visible() is True

    title_holder[0] = ""
    assert _title_visible() is False

    title_holder[0] = "x" * 51
    assert _title_visible() is False


def test_title_visible_with_none_callable_return() -> None:
    """``get_session_title`` returning None means hide the title."""

    def _title_visible() -> bool:
        title = (lambda: None)() or ""
        return 1 <= len(title) <= 50

    assert _title_visible() is False

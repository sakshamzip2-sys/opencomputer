"""The Ctrl+X Ctrl+R chord injects `/reasoning show` and submits."""
from __future__ import annotations

from opencomputer.cli_ui.input_loop import build_reasoning_show_handler


def test_handler_returns_slash_command_string():
    """The handler is the testable seam — when invoked, it returns the
    string the input_loop should inject as the user's next input."""
    out = build_reasoning_show_handler()()
    assert out == "/reasoning show"


def test_handler_factory_returns_callable():
    h = build_reasoning_show_handler()
    assert callable(h)

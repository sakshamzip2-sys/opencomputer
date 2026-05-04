"""Tests for bare ``?`` triggering /help (Wave 6.A — Hermes 7c0742220).

The hermes mini-help-menu UX: typing just ``?`` in the input shows the
command list without forcing the user to remember the slash. OC's port
piggybacks on the existing ``?`` alias of ``/help`` by widening
:func:`is_slash_command` to recognize a bare ``?``.
"""

from __future__ import annotations

from opencomputer.cli_ui.slash import (
    is_slash_command,
    resolve_command,
)
from opencomputer.cli_ui.slash_handlers import _split_args


def test_bare_question_mark_is_slash():
    assert is_slash_command("?") is True


def test_bare_question_mark_with_whitespace_is_slash():
    assert is_slash_command("  ?  ") is True


def test_question_mark_with_text_is_not_slash():
    """``?something`` is a typo / regular text, not the help shortcut."""
    assert is_slash_command("?abc") is False


def test_empty_string_is_not_slash():
    assert is_slash_command("") is False


def test_regular_text_is_not_slash():
    assert is_slash_command("hello") is False


def test_slash_alone_is_not_slash():
    assert is_slash_command("/") is False


def test_slash_with_command_still_slash():
    assert is_slash_command("/help") is True


def test_question_resolves_to_help():
    cmd = resolve_command("?")
    assert cmd is not None
    assert cmd.name == "help"


def test_split_args_on_bare_question_mark():
    name, args = _split_args("?")
    assert name == "?"
    assert args == []

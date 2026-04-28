"""Tests for SlashCommandCompleter and longest_common_prefix.

These tests cover the prompt_toolkit Completer that powers the dropdown
menu users see when typing slash commands in the OpenComputer TUI. The
completer reads SLASH_REGISTRY directly so there's no parallel registry
to keep in sync — adding/removing a command in slash.py is automatically
reflected in autocomplete.
"""
from __future__ import annotations

from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document

from opencomputer.cli_ui.slash_completer import (
    SlashCommandCompleter,
    longest_common_prefix,
)


def _completions(text: str) -> list[str]:
    completer = SlashCommandCompleter()
    doc = Document(text=text, cursor_position=len(text))
    return [c.text for c in completer.get_completions(doc, CompleteEvent())]


# ---- longest_common_prefix --------------------------------------------------


def test_lcp_empty_list():
    assert longest_common_prefix([]) == ""


def test_lcp_single_string():
    assert longest_common_prefix(["/help"]) == "/help"


def test_lcp_multiple_with_prefix():
    assert longest_common_prefix(["/clear", "/cost"]) == "/c"


def test_lcp_no_common_beyond_slash():
    assert longest_common_prefix(["/help", "/exit"]) == "/"


def test_lcp_case_sensitive():
    assert longest_common_prefix(["/Help", "/help"]) == "/"


# ---- SlashCommandCompleter --------------------------------------------------


def test_completer_no_slash_yields_nothing():
    assert _completions("hello") == []


def test_completer_empty_yields_nothing():
    assert _completions("") == []


def test_completer_only_slash_yields_all_canonical_commands():
    out = _completions("/")
    assert "/exit" in out
    assert "/clear" in out
    assert "/help" in out
    assert "/rename" in out
    assert "/resume" in out
    assert "/q" not in out
    assert "/quit" not in out
    assert "/h" not in out
    assert "/?" not in out


def test_completer_prefix_filters_by_name():
    assert _completions("/cl") == ["/clear"]


def test_completer_prefix_re_returns_rename_and_resume():
    out = _completions("/re")
    assert out == ["/rename", "/resume"]


def test_completer_prefix_case_insensitive():
    assert _completions("/HE") == ["/help"]
    assert _completions("/Re") == ["/rename", "/resume"]


def test_completer_no_match_returns_empty():
    assert _completions("/zzz") == []


def test_completer_aborts_after_space():
    assert _completions("/help ") == []
    assert _completions("/rename foo") == []


def test_completer_returns_completion_with_meta_and_start_position():
    completer = SlashCommandCompleter()
    doc = Document(text="/help", cursor_position=5)
    [c] = list(completer.get_completions(doc, CompleteEvent()))
    assert c.text == "/help"
    assert "slash commands" in c.display_meta_text.lower()
    assert c.start_position == -len("/help")


def _display_plain(c) -> str:
    """Extract the plain rendered text from a Completion's display."""
    return "".join(text for _style, text in c.display)


def test_completer_display_includes_args_hint_and_category_for_rename():
    completer = SlashCommandCompleter()
    doc = Document(text="/rename", cursor_position=7)
    [c] = list(completer.get_completions(doc, CompleteEvent()))
    plain = _display_plain(c)
    assert "rename" in plain
    assert "<new title>" in plain
    assert "(session)" in plain


def test_completer_display_omits_hint_but_includes_category_for_argless():
    completer = SlashCommandCompleter()
    doc = Document(text="/help", cursor_position=5)
    [c] = list(completer.get_completions(doc, CompleteEvent()))
    plain = _display_plain(c)
    assert "/help" in plain
    assert "(meta)" in plain
    assert "<" not in plain


def test_completer_results_sorted_alphabetically():
    out = _completions("/")
    assert out == sorted(out)


def test_completer_double_slash_yields_nothing():
    assert _completions("//") == []

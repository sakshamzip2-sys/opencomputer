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
    # PR #234 added /reload; assert /rename and /resume are present alongside.
    assert "/rename" in out
    assert "/resume" in out


def test_completer_prefix_case_insensitive():
    assert _completions("/HE") == ["/help"]
    out = _completions("/Re")
    assert "/rename" in out
    assert "/resume" in out


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


def test_completer_display_includes_args_hint_and_source_tag_for_rename():
    """Updated for Claude Code parity (Task 6): the (category) tag was
    replaced by a (command)/(skill) source tag — see spec §3.5."""
    completer = SlashCommandCompleter()
    doc = Document(text="/rename", cursor_position=7)
    [c] = list(completer.get_completions(doc, CompleteEvent()))
    plain = _display_plain(c)
    assert "rename" in plain
    assert "<new title>" in plain
    assert "(command)" in plain


def test_completer_display_omits_hint_but_includes_source_tag_for_argless():
    """Updated for Claude Code parity (Task 6): unified (command) tag."""
    completer = SlashCommandCompleter()
    doc = Document(text="/help", cursor_position=5)
    [c] = list(completer.get_completions(doc, CompleteEvent()))
    plain = _display_plain(c)
    assert "/help" in plain
    assert "(command)" in plain
    assert "<" not in plain


def test_completer_results_sorted_alphabetically():
    out = _completions("/")
    assert out == sorted(out)


def test_completer_double_slash_yields_nothing():
    assert _completions("//") == []


# ─── Task 6: source-aware completer ────────────────────────────────


def test_completer_yields_skills_when_source_provided(tmp_path) -> None:
    """When constructed with a UnifiedSlashSource, the completer yields
    skill rows alongside command rows."""
    from opencomputer.cli_ui.slash_completer import SlashCommandCompleter
    from opencomputer.cli_ui.slash_mru import MruStore
    from opencomputer.cli_ui.slash_picker_source import UnifiedSlashSource

    class _Fake:
        def list_skills(self):
            from dataclasses import dataclass

            @dataclass
            class _M:
                id: str
                name: str
                description: str = ""

            return [_M(id="my-skill", name="My Skill", description="x")]

    src = UnifiedSlashSource(_Fake(), MruStore(tmp_path / "mru.json"))
    comp = SlashCommandCompleter(source=src)
    completions = list(comp.get_completions(Document("/my"), CompleteEvent()))
    texts = [c.text for c in completions]
    assert "/my-skill" in texts


def test_completer_truncates_long_descriptions_at_250_chars() -> None:
    """Spec §3.6 — descriptions over 250 chars are word-boundary trimmed
    with ellipsis. Whitespace (newlines, tabs, multi-space) is normalized
    to single spaces before trimming so YAML frontmatter multi-line
    descriptions don't break the dropdown columns (BLOCKER B1)."""
    from opencomputer.cli_ui.slash_completer import _trim_description

    long = "this is a long description " * 20  # ~ 540 chars
    trimmed = _trim_description(long)
    assert len(trimmed) <= 251  # 250 + 1 for the ellipsis
    assert trimmed.endswith("…")
    # Trimming happens at a word boundary — never mid-word.
    assert not trimmed[:-1].endswith(" ")
    # Short descriptions are returned with whitespace normalized.
    assert _trim_description("short") == "short"
    assert _trim_description("") == ""
    # Newlines and runs of whitespace get collapsed.
    assert _trim_description("line one\nline two") == "line one line two"
    assert _trim_description("a   b\t\tc") == "a b c"
    assert _trim_description("  leading and trailing  ") == "leading and trailing"


def test_completer_renders_source_tag_in_display(tmp_path) -> None:
    """Display should mark commands as `(command)` and skills as
    `(skill)` so the user can tell them apart."""
    from opencomputer.cli_ui.slash_completer import SlashCommandCompleter
    from opencomputer.cli_ui.slash_mru import MruStore
    from opencomputer.cli_ui.slash_picker_source import UnifiedSlashSource

    class _Fake:
        def list_skills(self):
            from dataclasses import dataclass

            @dataclass
            class _M:
                id: str
                name: str
                description: str = "skill desc"

            return [_M(id="my-skill", name="My Skill")]

    src = UnifiedSlashSource(_Fake(), MruStore(tmp_path / "mru.json"))
    comp = SlashCommandCompleter(source=src)
    completions = list(comp.get_completions(Document("/"), CompleteEvent()))
    by_text = {c.text: c for c in completions}
    # Command row.
    assert "(command)" in str(by_text["/help"].display)
    # Skill row.
    assert "(skill)" in str(by_text["/my-skill"].display)

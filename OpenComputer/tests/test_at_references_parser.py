"""At-reference grammar parser."""
from __future__ import annotations

from opencomputer.agent.at_references import AtRef, parse


def test_parses_simple_file_ref():
    refs = parse("look at @file:foo/bar.py please")
    assert refs == [
        AtRef(kind="file", arg="foo/bar.py", line_start=None, line_end=None)
    ]


def test_parses_file_with_line_range():
    refs = parse("@file:src/main.py:10-25")
    assert refs == [
        AtRef(kind="file", arg="src/main.py", line_start=10, line_end=25)
    ]


def test_parses_folder_ref():
    refs = parse("see @folder:src for context")
    assert refs == [
        AtRef(kind="folder", arg="src", line_start=None, line_end=None)
    ]


def test_parses_diff():
    refs = parse("@diff")
    assert refs == [
        AtRef(kind="diff", arg="", line_start=None, line_end=None)
    ]


def test_parses_staged():
    refs = parse("@staged")
    assert refs == [
        AtRef(kind="staged", arg="", line_start=None, line_end=None)
    ]


def test_parses_git_with_count():
    refs = parse("@git:5")
    assert refs == [
        AtRef(kind="git", arg="5", line_start=None, line_end=None)
    ]


def test_parses_url():
    refs = parse("@url:https://example.com/foo")
    assert refs == [
        AtRef(
            kind="url",
            arg="https://example.com/foo",
            line_start=None,
            line_end=None,
        )
    ]


def test_parses_multiple_in_one_message():
    refs = parse("compare @file:a.py with @file:b.py")
    assert len(refs) == 2
    assert refs[0].arg == "a.py"
    assert refs[1].arg == "b.py"


def test_email_address_is_not_an_atref():
    refs = parse("ping me at sak@example.com")
    assert refs == []


def test_at_alone_is_not_an_atref():
    refs = parse("hi @ there")
    assert refs == []


def test_strips_trailing_punctuation():
    refs = parse("see @file:foo.py, then think.")
    assert refs == [
        AtRef(kind="file", arg="foo.py", line_start=None, line_end=None)
    ]


def test_atref_is_frozen():
    import pytest
    ref = AtRef(kind="file", arg="x", line_start=None, line_end=None)
    with pytest.raises(Exception):
        ref.arg = "y"  # type: ignore[misc]

"""Tests for cli_ui.markdown_strip — preserve code, strip markup."""

from opencomputer.cli_ui.markdown_strip import strip_for_terminal


def test_bold_stripped() -> None:
    assert strip_for_terminal("the **quick** brown fox") == "the quick brown fox"


def test_italic_stripped_star_and_underscore() -> None:
    assert strip_for_terminal("an *italic* word") == "an italic word"
    assert strip_for_terminal("an _italic_ word") == "an italic word"


def test_atx_heading_markers_stripped() -> None:
    assert strip_for_terminal("# Heading\nbody") == "Heading\nbody"
    assert strip_for_terminal("## Sub") == "Sub"


def test_code_fence_preserved_verbatim() -> None:
    md = "before\n```python\n**not bold here**\n```\nafter"
    out = strip_for_terminal(md)
    assert "**not bold here**" in out
    assert out.startswith("before")
    assert out.endswith("after")


def test_inline_code_preserved() -> None:
    md = "use `**literal**` to bold"
    out = strip_for_terminal(md)
    assert "`**literal**`" in out


def test_list_markers_preserved() -> None:
    md = "- item one\n- **bold** item\n  - nested\n1. ordered"
    out = strip_for_terminal(md)
    assert "- item one" in out
    assert "- bold item" in out  # bold stripped, dash preserved
    assert "1. ordered" in out


def test_table_pipes_preserved() -> None:
    md = "| col |\n|-----|\n| **bold** |"
    out = strip_for_terminal(md)
    assert "| col |" in out
    # The bold strip happens on the table row's literal content but pipes survive
    assert "|" in out


def test_link_url_preserved() -> None:
    out = strip_for_terminal("see [docs](https://example.com)")
    assert "https://example.com" in out


def test_idempotent() -> None:
    md = "the **quick** brown fox"
    once = strip_for_terminal(md)
    twice = strip_for_terminal(once)
    assert once == twice


def test_underscore_in_identifier_not_treated_as_italic() -> None:
    md = "use my_variable_name to debug"
    out = strip_for_terminal(md)
    assert "my_variable_name" in out

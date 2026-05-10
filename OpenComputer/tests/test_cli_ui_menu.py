"""Tests for cli_ui/menu.py — arrow-key + numbered-fallback menu primitives."""
from __future__ import annotations

import io

import pytest


def test_choice_dataclass_holds_label_value_and_optional_description():
    from opencomputer.cli_ui.menu import Choice

    c = Choice(label="Anthropic", value="anthropic", description="Claude models")
    assert c.label == "Anthropic"
    assert c.value == "anthropic"
    assert c.description == "Claude models"

    c2 = Choice(label="OpenAI", value="openai")
    assert c2.description is None


def test_menu_style_forces_default_background_without_reverse_video():
    """Selected/menu text should be green-on-terminal-bg, not grey reverse video."""
    from opencomputer.cli_ui.style import MENU_STYLE

    for style_name in (
        "menu.title",
        "menu.hint",
        "menu.selected",
        "menu.selected.arrow",
        "menu.selected.glyph",
        "menu.unselected.glyph",
        "menu.description",
    ):
        attrs = MENU_STYLE.get_attrs_for_style_str(f"class:{style_name}")
        assert attrs.bgcolor == "ansidefault"
        assert attrs.reverse is False


def test_menu_window_hides_cursor_and_does_not_extend_highlight_area():
    """The prompt-toolkit cursor line must not paint a grey slab behind rows."""
    from prompt_toolkit.formatted_text import FormattedText

    from opencomputer.cli_ui.menu import _menu_window

    window = _menu_window(lambda: FormattedText([("", "x")]))

    assert window.always_hide_cursor()
    assert window.dont_extend_width()
    assert window.content.show_cursor is False


def test_menu_application_does_not_open_nested_alternate_buffer():
    from prompt_toolkit.formatted_text import FormattedText
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.output import DummyOutput

    from opencomputer.cli_ui.menu import _menu_application
    from opencomputer.cli_ui.style import MENU_STYLE

    app = _menu_application(
        lambda: FormattedText([("", "x")]),
        KeyBindings(),
        style=MENU_STYLE,
        _input=None,
        _output=DummyOutput(),
    )

    assert app.renderer.full_screen is False


def test_menu_uses_prompt_toolkit_when_windows_stdout_is_tty(monkeypatch):
    """Windows launchers can expose a TTY stdout while stdin reports non-TTY."""
    from opencomputer.cli_ui import menu

    monkeypatch.setattr(menu, "_IS_WINDOWS", True)
    monkeypatch.setattr(menu.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(menu.sys.stdout, "isatty", lambda: True)

    assert menu._should_use_prompt_toolkit(_input=None) is True


def test_radiolist_numbered_fallback_returns_index_for_valid_input(monkeypatch, capsys):
    """When stdin is non-TTY, radiolist falls back to a numbered prompt."""
    from opencomputer.cli_ui.menu import Choice, radiolist

    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("sys.stdin", io.StringIO("2\n"))

    choices = [
        Choice("Anthropic", "anthropic"),
        Choice("OpenAI", "openai"),
        Choice("OpenRouter", "openrouter"),
    ]

    idx = radiolist("Select provider:", choices, default=0)

    assert idx == 1, "Numbered input '2' → index 1 (1-based menu, 0-based return)"
    out = capsys.readouterr().out
    assert "Anthropic" in out and "OpenAI" in out and "OpenRouter" in out


def test_radiolist_numbered_fallback_empty_input_returns_default(monkeypatch):
    """Empty input on the numbered fallback returns the configured default."""
    from opencomputer.cli_ui.menu import Choice, radiolist

    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("sys.stdin", io.StringIO("\n"))

    choices = [Choice("A", "a"), Choice("B", "b"), Choice("C", "c")]
    idx = radiolist("Pick:", choices, default=2)
    assert idx == 2


def test_radiolist_numbered_fallback_invalid_then_valid(monkeypatch, capsys):
    """Invalid number re-prompts; valid second answer is accepted."""
    from opencomputer.cli_ui.menu import Choice, radiolist

    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("sys.stdin", io.StringIO("9\n1\n"))

    choices = [Choice("A", "a"), Choice("B", "b")]
    idx = radiolist("Pick:", choices)
    assert idx == 0
    err = capsys.readouterr().err
    assert "out of range" in err.lower() or "invalid" in err.lower()


def test_radiolist_tty_arrow_down_then_enter_selects_next(monkeypatch):
    """Pipe input simulates a TTY user pressing Down + Enter."""
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from opencomputer.cli_ui.menu import Choice, radiolist

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    with create_pipe_input() as pipe_input:
        pipe_input.send_text("\x1b[B\r")  # Down, Enter
        idx = radiolist(
            "Pick:",
            [Choice("A", "a"), Choice("B", "b"), Choice("C", "c")],
            default=0,
            _input=pipe_input,
            _output=DummyOutput(),
        )

    assert idx == 1, "Down from default 0 → index 1"


def test_radiolist_tty_immediate_enter_returns_default(monkeypatch):
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from opencomputer.cli_ui.menu import Choice, radiolist

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    with create_pipe_input() as pipe_input:
        pipe_input.send_text("\r")
        idx = radiolist(
            "Pick:",
            [Choice("A", "a"), Choice("B", "b")],
            default=1,
            _input=pipe_input,
            _output=DummyOutput(),
        )

    assert idx == 1


def test_radiolist_tty_number_then_enter_selects_numbered_row(monkeypatch):
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from opencomputer.cli_ui.menu import Choice, radiolist

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    with create_pipe_input() as pipe_input:
        pipe_input.send_text("2\r")
        idx = radiolist(
            "Pick:",
            [Choice("A", "a"), Choice("B", "b"), Choice("C", "c")],
            default=0,
            _input=pipe_input,
            _output=DummyOutput(),
        )

    assert idx == 1


def test_radiolist_tty_multi_digit_number_then_enter(monkeypatch):
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from opencomputer.cli_ui.menu import Choice, radiolist

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    choices = [Choice(f"Item {i}", i) for i in range(1, 13)]

    with create_pipe_input() as pipe_input:
        pipe_input.send_text("12\r")
        idx = radiolist(
            "Pick:",
            choices,
            default=0,
            _input=pipe_input,
            _output=DummyOutput(),
        )

    assert idx == 11


def test_radiolist_tty_esc_raises_wizard_cancelled(monkeypatch):
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from opencomputer.cli_ui.menu import Choice, WizardCancelled, radiolist

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    with create_pipe_input() as pipe_input:
        pipe_input.send_text("\x1b")  # ESC
        with pytest.raises(WizardCancelled):
            radiolist(
                "Pick:",
                [Choice("A", "a")],
                _input=pipe_input,
                _output=DummyOutput(),
            )


def test_checklist_numbered_fallback_returns_selected_indices(monkeypatch):
    from opencomputer.cli_ui.menu import Choice, checklist

    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("sys.stdin", io.StringIO("1,3\n"))

    items = [Choice("Telegram", "telegram"), Choice("Discord", "discord"),
             Choice("Slack", "slack"), Choice("Matrix", "matrix")]
    selected = checklist("Select platforms:", items)
    assert selected == [0, 2]


def test_checklist_numbered_fallback_pre_selected_default(monkeypatch):
    from opencomputer.cli_ui.menu import Choice, checklist

    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("sys.stdin", io.StringIO("\n"))

    items = [Choice("A", "a"), Choice("B", "b")]
    selected = checklist("Pick:", items, pre_selected=[1])
    assert selected == [1]


def test_checklist_tty_space_toggles_then_enter_confirms(monkeypatch):
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from opencomputer.cli_ui.menu import Choice, checklist

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    with create_pipe_input() as pipe_input:
        # Default cursor at 0; SPACE toggles 0; Down; SPACE toggles 1; Enter.
        pipe_input.send_text(" \x1b[B \r")
        selected = checklist(
            "Pick:",
            [Choice("A", "a"), Choice("B", "b"), Choice("C", "c")],
            _input=pipe_input,
            _output=DummyOutput(),
        )
    assert selected == [0, 1]


def test_checklist_tty_number_moves_cursor_then_space_toggles(monkeypatch):
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from opencomputer.cli_ui.menu import Choice, checklist

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    items = [Choice(f"Item {i}", i) for i in range(1, 13)]

    with create_pipe_input() as pipe_input:
        pipe_input.send_text("12 \r")
        selected = checklist(
            "Pick:",
            items,
            _input=pipe_input,
            _output=DummyOutput(),
        )

    assert selected == [11]


def test_checklist_tty_esc_raises_wizard_cancelled(monkeypatch):
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from opencomputer.cli_ui.menu import Choice, WizardCancelled, checklist

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    with create_pipe_input() as pipe_input:
        pipe_input.send_text("\x1b")
        with pytest.raises(WizardCancelled):
            checklist(
                "Pick:",
                [Choice("A", "a")],
                _input=pipe_input,
                _output=DummyOutput(),
            )


def test_single_select_numbered_fallback_returns_index(monkeypatch):
    from opencomputer.cli_ui.menu import Choice, single_select

    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("sys.stdin", io.StringIO("3\n"))

    items = [Choice("A", "a"), Choice("B", "b"), Choice("C", "c")]
    idx = single_select("Pick:", items, default=0)
    assert idx == 2


def test_single_select_tty_arrow_then_enter(monkeypatch):
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from opencomputer.cli_ui.menu import Choice, single_select

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    with create_pipe_input() as pipe_input:
        pipe_input.send_text("\x1b[B\x1b[B\r")  # Down, Down, Enter
        idx = single_select(
            "Pick:",
            [Choice("A", "a"), Choice("B", "b"), Choice("C", "c")],
            default=0, _input=pipe_input, _output=DummyOutput(),
        )
    assert idx == 2


def test_single_select_tty_number_then_enter(monkeypatch):
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from opencomputer.cli_ui.menu import Choice, single_select

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    with create_pipe_input() as pipe_input:
        pipe_input.send_text("3\r")
        idx = single_select(
            "Pick:",
            [Choice("A", "a"), Choice("B", "b"), Choice("C", "c")],
            default=0,
            _input=pipe_input,
            _output=DummyOutput(),
        )
    assert idx == 2

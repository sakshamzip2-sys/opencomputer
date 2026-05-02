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

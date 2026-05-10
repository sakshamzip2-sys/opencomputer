"""Tests for cli_ui.theme_detect."""

from opencomputer.cli_ui.theme_detect import detect_theme


def test_env_override_light(monkeypatch) -> None:
    monkeypatch.setenv("OPENCOMPUTER_TUI_THEME", "light")
    monkeypatch.delenv("COLORFGBG", raising=False)
    assert detect_theme(probe=lambda: None).kind == "light"


def test_env_override_dark(monkeypatch) -> None:
    monkeypatch.setenv("OPENCOMPUTER_TUI_THEME", "dark")
    monkeypatch.delenv("COLORFGBG", raising=False)
    assert detect_theme(probe=lambda: None).kind == "dark"


def test_env_override_hex_bg(monkeypatch) -> None:
    monkeypatch.setenv("OPENCOMPUTER_TUI_THEME", "ffffff")
    monkeypatch.delenv("COLORFGBG", raising=False)
    t = detect_theme(probe=lambda: None)
    assert t.kind == "light"
    assert t.bg_hex == "ffffff"


def test_colorfgbg_xterm_light(monkeypatch) -> None:
    monkeypatch.delenv("OPENCOMPUTER_TUI_THEME", raising=False)
    monkeypatch.setenv("COLORFGBG", "0;15")
    assert detect_theme(probe=lambda: None).kind == "light"


def test_colorfgbg_xterm_dark(monkeypatch) -> None:
    monkeypatch.delenv("OPENCOMPUTER_TUI_THEME", raising=False)
    monkeypatch.setenv("COLORFGBG", "15;0")
    assert detect_theme(probe=lambda: None).kind == "dark"


def test_osc11_probe_light(monkeypatch) -> None:
    monkeypatch.delenv("OPENCOMPUTER_TUI_THEME", raising=False)
    monkeypatch.delenv("COLORFGBG", raising=False)
    reply = "\x1b]11;rgb:ffff/ffff/ffff\x1b\\"
    assert detect_theme(probe=lambda: reply).kind == "light"


def test_osc11_probe_dark(monkeypatch) -> None:
    monkeypatch.delenv("OPENCOMPUTER_TUI_THEME", raising=False)
    monkeypatch.delenv("COLORFGBG", raising=False)
    reply = "\x1b]11;rgb:1010/1010/1010\x1b\\"
    assert detect_theme(probe=lambda: reply).kind == "dark"


def test_default_dark_when_all_silent(monkeypatch) -> None:
    monkeypatch.delenv("OPENCOMPUTER_TUI_THEME", raising=False)
    monkeypatch.delenv("COLORFGBG", raising=False)
    assert detect_theme(probe=lambda: None).kind == "dark"


def test_probe_timeout_returns_none(monkeypatch) -> None:
    """A probe that returns None falls back to dark."""
    monkeypatch.delenv("OPENCOMPUTER_TUI_THEME", raising=False)
    monkeypatch.delenv("COLORFGBG", raising=False)
    assert detect_theme(probe=lambda: None).kind == "dark"


def test_invalid_colorfgbg_falls_through(monkeypatch) -> None:
    monkeypatch.delenv("OPENCOMPUTER_TUI_THEME", raising=False)
    monkeypatch.setenv("COLORFGBG", "garbage")
    assert detect_theme(probe=lambda: None).kind == "dark"


def test_env_override_invalid_hex_ignored(monkeypatch) -> None:
    monkeypatch.setenv("OPENCOMPUTER_TUI_THEME", "zzzzzz")
    monkeypatch.delenv("COLORFGBG", raising=False)
    # Falls through to dark default.
    assert detect_theme(probe=lambda: None).kind == "dark"

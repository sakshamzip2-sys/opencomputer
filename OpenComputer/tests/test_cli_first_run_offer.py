"""First-run onboarding offer for ``opencomputer chat`` (hermes parity).

Hermes' ``main.py`` checks ``_has_any_provider_configured()`` at chat
launch and, if no provider key is reachable, prints an inline
``Run setup now? [Y/n]`` prompt that hands off to the wizard. OC was
exiting with a static export hint instead — these tests pin the new
behaviour.
"""
from __future__ import annotations

import io
from unittest.mock import patch

import pytest
import typer


def test_require_tty_exits_when_stdin_is_pipe(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``_require_tty`` exits 1 with a clear stderr message on non-TTY stdin."""
    from opencomputer import cli

    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    with pytest.raises(SystemExit) as exc:
        cli._require_tty("setup")
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "interactive terminal" in err
    assert "opencomputer setup" in err


def test_require_tty_passes_when_stdin_is_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    from opencomputer import cli

    class FakeTTY:
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr("sys.stdin", FakeTTY())
    cli._require_tty("setup")


def test_has_any_provider_configured_true_with_anthropic_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opencomputer import cli

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("AWS_BEDROCK_ACCESS_KEY_ID", raising=False)
    assert cli._has_any_provider_configured() is True


def test_has_any_provider_configured_true_with_openai_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opencomputer import cli

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert cli._has_any_provider_configured() is True


def test_has_any_provider_configured_false_when_no_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opencomputer import cli

    for var in (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "AWS_BEDROCK_ACCESS_KEY_ID",
        "ANTHROPIC_BASE_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    assert cli._has_any_provider_configured() is False


def test_offer_setup_inline_yes_runs_wizard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opencomputer import cli

    class FakeTTY:
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr("sys.stdin", FakeTTY())
    monkeypatch.setattr("builtins.input", lambda _: "y")

    setup_called: list[bool] = []

    def fake_run_setup() -> None:
        setup_called.append(True)

    monkeypatch.setattr(
        "opencomputer.setup_wizard.run_setup", fake_run_setup
    )
    with pytest.raises(typer.Exit) as exc:
        cli._offer_setup_or_exit("ANTHROPIC_API_KEY is not set")
    assert exc.value.exit_code == 0
    assert setup_called == [True]


def test_offer_setup_inline_no_exits_with_hint(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from opencomputer import cli

    class FakeTTY:
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr("sys.stdin", FakeTTY())
    monkeypatch.setattr("builtins.input", lambda _: "n")
    with pytest.raises(typer.Exit) as exc:
        cli._offer_setup_or_exit("Config not found")
    assert exc.value.exit_code == 1
    out = capsys.readouterr().out
    assert "opencomputer setup" in out


def test_offer_setup_in_non_tty_prints_static_guidance(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from opencomputer import cli

    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    with pytest.raises(typer.Exit) as exc:
        cli._offer_setup_or_exit("Config not found")
    assert exc.value.exit_code == 1
    out = capsys.readouterr().out
    assert "opencomputer setup" in out

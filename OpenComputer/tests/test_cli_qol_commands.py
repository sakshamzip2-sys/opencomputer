"""QoL commands ported from hermes-agent.

Two small commands that close paper-cut gaps in OC's CLI surface:

- ``opencomputer config edit`` — opens config.yaml in $EDITOR.
  Mirrors hermes' ``hermes config edit`` (referenced in the wizard at
  ``setup.py:2207``).
- ``opencomputer auth`` — focused provider-credential view. Like
  ``hermes auth status`` (referenced from
  ``main.py:_has_any_provider_configured``) — shows which providers'
  primary env vars are set, last-4 only, never echoes a full token.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# ── config edit ────────────────────────────────────────────────────────

def test_config_edit_invokes_editor_with_config_path(
    tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``config edit`` calls subprocess.run([$EDITOR, <config-path>])."""
    from opencomputer import cli

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("model:\n  provider: anthropic\n")
    monkeypatch.setattr(cli, "config_file_path", lambda: cfg_path)
    monkeypatch.setenv("EDITOR", "fake-editor")

    captured: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        captured.append(list(cmd))

        class _R:
            returncode = 0

        return _R()

    monkeypatch.setattr("subprocess.run", fake_run)

    result = runner.invoke(cli.app, ["config", "edit"])

    assert result.exit_code == 0
    assert captured == [["fake-editor", str(cfg_path)]]


def test_config_edit_falls_back_to_vi_when_editor_unset(
    tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without $EDITOR, fall back to a sensible default (vi on POSIX)."""
    from opencomputer import cli

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("model:\n  provider: anthropic\n")
    monkeypatch.setattr(cli, "config_file_path", lambda: cfg_path)
    monkeypatch.delenv("EDITOR", raising=False)
    monkeypatch.delenv("VISUAL", raising=False)

    captured: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        captured.append(list(cmd))

        class _R:
            returncode = 0

        return _R()

    monkeypatch.setattr("subprocess.run", fake_run)

    result = runner.invoke(cli.app, ["config", "edit"])

    assert result.exit_code == 0
    assert captured[0][0] == "vi"
    assert captured[0][1] == str(cfg_path)


def test_config_edit_prefers_visual_over_editor(
    tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POSIX convention: $VISUAL beats $EDITOR when both are set."""
    from opencomputer import cli

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("model:\n  provider: anthropic\n")
    monkeypatch.setattr(cli, "config_file_path", lambda: cfg_path)
    monkeypatch.setenv("EDITOR", "fake-editor")
    monkeypatch.setenv("VISUAL", "fake-visual")

    captured: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        captured.append(list(cmd))

        class _R:
            returncode = 0

        return _R()

    monkeypatch.setattr("subprocess.run", fake_run)

    runner.invoke(cli.app, ["config", "edit"])

    assert captured[0][0] == "fake-visual"


def test_config_edit_refuses_when_config_missing(
    tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If config.yaml doesn't exist, suggest ``opencomputer setup``
    instead of opening an empty file the user has to populate by hand."""
    from opencomputer import cli

    cfg_path = tmp_path / "config.yaml"
    monkeypatch.setattr(cli, "config_file_path", lambda: cfg_path)

    result = runner.invoke(cli.app, ["config", "edit"])

    assert result.exit_code != 0
    assert "opencomputer setup" in result.stdout.lower()


# ── auth ───────────────────────────────────────────────────────────────

def test_auth_lists_anthropic_when_env_set(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The auth command shows ANTHROPIC_API_KEY as set with last-4."""
    from opencomputer import cli

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-1234567890ABCD")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)

    result = runner.invoke(cli.app, ["auth"])

    assert result.exit_code == 0
    out_lower = result.stdout.lower()
    assert "anthropic_api_key" in out_lower
    assert "abcd" in out_lower or "abcd" in result.stdout
    assert "1234567890ab" not in result.stdout, (
        "auth must NOT echo the full token — last-4 only"
    )


def test_auth_marks_missing_keys_as_not_set(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing keys are listed too — gives the user a quick checklist."""
    from opencomputer import cli

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)

    result = runner.invoke(cli.app, ["auth"])

    assert result.exit_code == 0
    assert "not set" in result.stdout.lower()


def test_auth_includes_anthropic_base_url_when_set(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ANTHROPIC_BASE_URL (proxy mode) is surfaced in auth output."""
    from opencomputer import cli

    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://claude-router.example.com")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "abcd")

    result = runner.invoke(cli.app, ["auth"])

    assert result.exit_code == 0
    assert "claude-router.example.com" in result.stdout

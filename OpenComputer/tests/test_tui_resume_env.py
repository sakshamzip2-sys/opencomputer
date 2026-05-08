"""Tests for ``oc tui`` resume-spec computation — Hermes-parity ``OPENCOMPUTER_TUI_RESUME``.

The TUI launcher (``cli_tui.run``) translates user intent into the
``OC_TUI_RESUME`` env var that the Ink/React TUI consumes (in entry.tsx).
Precedence: explicit ``--resume <id>`` > ``--continue`` > env var.

Doesn't actually launch Node — patches ``os.execvpe`` to capture the env
that would be exported.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from opencomputer.cli_tui import tui_app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _stub_entry_path(tmp_path: Path) -> Path:
    """Create a fake dist/entry.js so the existence check passes."""
    entry = tmp_path / "ui-tui" / "dist" / "entry.js"
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text("// stub\n")
    return entry


def _capture_env() -> dict[str, dict]:
    """Patch shutil.which + os.execvpe to capture the env that would be exec'd.

    Returns a dict the test can inspect after invoking the runner.
    """
    captured: dict[str, dict] = {}

    def _fake_execvpe(prog, argv, env):
        captured["prog"] = prog
        captured["argv"] = argv
        captured["env"] = env
        # Don't actually exec — return so the typer command finishes.

    return captured, _fake_execvpe


def test_continue_flag_sets_oc_tui_resume_last(runner, tmp_path, monkeypatch):
    """``oc tui --continue`` should export OC_TUI_RESUME=last."""
    entry = _stub_entry_path(tmp_path)
    captured, fake = _capture_env()
    monkeypatch.delenv("OPENCOMPUTER_TUI_RESUME", raising=False)
    with (
        patch("opencomputer.cli_tui._entry_path", return_value=entry),
        patch("opencomputer.cli_tui.shutil.which", return_value="/usr/bin/node"),
        patch("opencomputer.cli_tui.os.execvpe", side_effect=fake),
    ):
        result = runner.invoke(tui_app, ["--continue"])
    assert result.exit_code == 0
    assert captured["env"]["OC_TUI_RESUME"] == "last"


def test_short_c_flag_sets_oc_tui_resume_last(runner, tmp_path, monkeypatch):
    """``oc tui -c`` is the short form of --continue."""
    entry = _stub_entry_path(tmp_path)
    captured, fake = _capture_env()
    monkeypatch.delenv("OPENCOMPUTER_TUI_RESUME", raising=False)
    with (
        patch("opencomputer.cli_tui._entry_path", return_value=entry),
        patch("opencomputer.cli_tui.shutil.which", return_value="/usr/bin/node"),
        patch("opencomputer.cli_tui.os.execvpe", side_effect=fake),
    ):
        result = runner.invoke(tui_app, ["-c"])
    assert result.exit_code == 0
    assert captured["env"]["OC_TUI_RESUME"] == "last"


def test_resume_flag_with_session_id_passes_through(runner, tmp_path, monkeypatch):
    """``oc tui --resume abc123`` should export OC_TUI_RESUME=abc123."""
    entry = _stub_entry_path(tmp_path)
    captured, fake = _capture_env()
    monkeypatch.delenv("OPENCOMPUTER_TUI_RESUME", raising=False)
    with (
        patch("opencomputer.cli_tui._entry_path", return_value=entry),
        patch("opencomputer.cli_tui.shutil.which", return_value="/usr/bin/node"),
        patch("opencomputer.cli_tui.os.execvpe", side_effect=fake),
    ):
        result = runner.invoke(tui_app, ["--resume", "abc123"])
    assert result.exit_code == 0
    assert captured["env"]["OC_TUI_RESUME"] == "abc123"


def test_env_var_only_triggers_resume_last(runner, tmp_path, monkeypatch):
    """Setting OPENCOMPUTER_TUI_RESUME=1 (or true/yes) should trigger auto-resume latest."""
    entry = _stub_entry_path(tmp_path)
    captured, fake = _capture_env()
    monkeypatch.setenv("OPENCOMPUTER_TUI_RESUME", "1")
    with (
        patch("opencomputer.cli_tui._entry_path", return_value=entry),
        patch("opencomputer.cli_tui.shutil.which", return_value="/usr/bin/node"),
        patch("opencomputer.cli_tui.os.execvpe", side_effect=fake),
    ):
        result = runner.invoke(tui_app, [])
    assert result.exit_code == 0
    assert captured["env"]["OC_TUI_RESUME"] == "last"
    # The internal env var should not leak into the Node child process.
    assert "OPENCOMPUTER_TUI_RESUME" not in captured["env"]


def test_env_var_with_session_id_passes_through(runner, tmp_path, monkeypatch):
    """OPENCOMPUTER_TUI_RESUME=session-xyz should pass through as-is."""
    entry = _stub_entry_path(tmp_path)
    captured, fake = _capture_env()
    monkeypatch.setenv("OPENCOMPUTER_TUI_RESUME", "session-xyz")
    with (
        patch("opencomputer.cli_tui._entry_path", return_value=entry),
        patch("opencomputer.cli_tui.shutil.which", return_value="/usr/bin/node"),
        patch("opencomputer.cli_tui.os.execvpe", side_effect=fake),
    ):
        result = runner.invoke(tui_app, [])
    assert result.exit_code == 0
    assert captured["env"]["OC_TUI_RESUME"] == "session-xyz"


def test_explicit_resume_beats_continue_beats_env(runner, tmp_path, monkeypatch):
    """Precedence: --resume <id> > --continue > OPENCOMPUTER_TUI_RESUME."""
    entry = _stub_entry_path(tmp_path)
    captured, fake = _capture_env()
    monkeypatch.setenv("OPENCOMPUTER_TUI_RESUME", "1")
    with (
        patch("opencomputer.cli_tui._entry_path", return_value=entry),
        patch("opencomputer.cli_tui.shutil.which", return_value="/usr/bin/node"),
        patch("opencomputer.cli_tui.os.execvpe", side_effect=fake),
    ):
        result = runner.invoke(tui_app, ["--continue", "--resume", "winner"])
    assert result.exit_code == 0
    assert captured["env"]["OC_TUI_RESUME"] == "winner"


def test_no_resume_target_means_no_oc_tui_resume_env(runner, tmp_path, monkeypatch):
    """Without flags or env var, OC_TUI_RESUME should NOT be exported (TUI starts fresh)."""
    entry = _stub_entry_path(tmp_path)
    captured, fake = _capture_env()
    monkeypatch.delenv("OPENCOMPUTER_TUI_RESUME", raising=False)
    with (
        patch("opencomputer.cli_tui._entry_path", return_value=entry),
        patch("opencomputer.cli_tui.shutil.which", return_value="/usr/bin/node"),
        patch("opencomputer.cli_tui.os.execvpe", side_effect=fake),
    ):
        result = runner.invoke(tui_app, [])
    assert result.exit_code == 0
    assert "OC_TUI_RESUME" not in captured["env"]

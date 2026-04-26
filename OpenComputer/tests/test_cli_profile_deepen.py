"""CLI tests for `opencomputer profile deepen`."""
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from opencomputer.cli_profile import profile_app

runner = CliRunner()


def test_deepen_command_exists():
    result = runner.invoke(profile_app, ["deepen", "--help"])
    assert result.exit_code == 0
    assert "deepen" in result.stdout.lower() or "Layer 3" in result.stdout


def test_deepen_runs_and_displays_summary(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    fake_result = type("R", (), {})()
    fake_result.window_processed_days = 30
    fake_result.artifacts_processed = 12
    fake_result.motifs_emitted = 5
    fake_result.elapsed_seconds = 1.5
    fake_result.skipped_reason = ""

    with patch("opencomputer.cli_profile.run_deepening", return_value=fake_result):
        result = runner.invoke(profile_app, ["deepen", "--force"])
    assert result.exit_code == 0
    assert "30" in result.stdout  # window
    assert "12" in result.stdout  # artifacts
    assert "5" in result.stdout   # motifs


def test_deepen_displays_skip_reason_when_not_idle(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    fake_result = type("R", (), {})()
    fake_result.window_processed_days = 0
    fake_result.artifacts_processed = 0
    fake_result.motifs_emitted = 0
    fake_result.elapsed_seconds = 0.1
    fake_result.skipped_reason = "CPU at 80%"

    with patch("opencomputer.cli_profile.run_deepening", return_value=fake_result):
        result = runner.invoke(profile_app, ["deepen"])
    assert result.exit_code == 0
    assert "CPU at 80%" in result.stdout or "skipped" in result.stdout.lower()

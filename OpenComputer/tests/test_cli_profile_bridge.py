"""Layered Awareness MVP — bridge CLI subcommand tests."""
from pathlib import Path

from typer.testing import CliRunner

from opencomputer.cli_profile import profile_app

runner = CliRunner()


def test_bridge_token_creates_and_prints(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    result = runner.invoke(profile_app, ["bridge", "token"])
    assert result.exit_code == 0
    # Token is URL-safe base64-ish, length > 32
    out = result.stdout.strip().splitlines()[-1]
    assert len(out) >= 32


def test_bridge_token_idempotent(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    first = runner.invoke(profile_app, ["bridge", "token"]).stdout.strip().splitlines()[-1]
    second = runner.invoke(profile_app, ["bridge", "token"]).stdout.strip().splitlines()[-1]
    assert first == second  # second call returns the existing token


def test_bridge_token_rotate(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    first = runner.invoke(profile_app, ["bridge", "token"]).stdout.strip().splitlines()[-1]
    second = runner.invoke(
        profile_app, ["bridge", "token", "--rotate"]
    ).stdout.strip().splitlines()[-1]
    assert first != second

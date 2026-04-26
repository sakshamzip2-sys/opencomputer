"""Layered Awareness MVP — bridge CLI subcommand tests."""
from pathlib import Path
from unittest.mock import MagicMock, patch

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


def test_bridge_status_reports_reachable(tmp_path: Path, monkeypatch):
    """Status REACHABLE when localhost connect succeeds."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    runner.invoke(profile_app, ["bridge", "token"])  # seed token

    fake_sock = MagicMock()
    fake_sock.connect.return_value = None  # connect succeeds (returns None)

    with patch("socket.socket", return_value=fake_sock):
        result = runner.invoke(profile_app, ["bridge", "status"])
    assert result.exit_code == 0
    assert "REACHABLE" in result.stdout
    assert "NOT REACHABLE" not in result.stdout
    assert "Bind port: 18791" in result.stdout


def test_bridge_status_reports_unreachable(tmp_path: Path, monkeypatch):
    """Status NOT REACHABLE when localhost connect raises OSError."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    runner.invoke(profile_app, ["bridge", "token"])  # seed token

    fake_sock = MagicMock()
    fake_sock.connect.side_effect = OSError("connection refused")

    with patch("socket.socket", return_value=fake_sock):
        result = runner.invoke(profile_app, ["bridge", "status"])
    assert result.exit_code == 0
    assert "NOT REACHABLE" in result.stdout

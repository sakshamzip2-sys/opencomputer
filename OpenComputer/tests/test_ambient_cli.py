"""tests/test_ambient_cli.py — Typer CLI smoke tests."""
from __future__ import annotations

import json
import time

import pytest
from typer.testing import CliRunner

from opencomputer.cli_ambient import app


def test_status_shows_disabled_when_state_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_PROFILE_HOME", str(tmp_path))
    result = CliRunner().invoke(app, ["status"])
    assert result.exit_code == 0
    assert "disabled" in result.output.lower() or "not enabled" in result.output.lower()


def test_on_writes_state_file(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_PROFILE_HOME", str(tmp_path))
    result = CliRunner().invoke(app, ["on"])
    assert result.exit_code == 0
    state_path = tmp_path / "ambient" / "state.json"
    assert state_path.exists()
    state = json.loads(state_path.read_text())
    assert state["enabled"] is True


def test_off_clears_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_PROFILE_HOME", str(tmp_path))
    CliRunner().invoke(app, ["on"])
    result = CliRunner().invoke(app, ["off"])
    assert result.exit_code == 0
    state = json.loads((tmp_path / "ambient" / "state.json").read_text())
    assert state["enabled"] is False


def test_pause_with_duration_sets_paused_until(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_PROFILE_HOME", str(tmp_path))
    CliRunner().invoke(app, ["on"])
    before = time.time()
    result = CliRunner().invoke(app, ["pause", "--duration", "1h"])
    assert result.exit_code == 0
    state = json.loads((tmp_path / "ambient" / "state.json").read_text())
    assert state["paused_until"] is not None
    # Should be ~now+1h, give some slack
    assert before + 3500 < state["paused_until"] < before + 3700


def test_pause_without_enabling_first_errors(tmp_path, monkeypatch):
    """Can't pause if not enabled — that would be confusing."""
    monkeypatch.setenv("OPENCOMPUTER_PROFILE_HOME", str(tmp_path))
    result = CliRunner().invoke(app, ["pause", "--duration", "1h"])
    assert result.exit_code != 0


def test_pause_indefinite_sets_far_future(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_PROFILE_HOME", str(tmp_path))
    CliRunner().invoke(app, ["on"])
    result = CliRunner().invoke(app, ["pause"])
    assert result.exit_code == 0
    state = json.loads((tmp_path / "ambient" / "state.json").read_text())
    # Far future — at least 1 year out
    assert state["paused_until"] > time.time() + 365 * 86400


def test_resume_clears_paused_until(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_PROFILE_HOME", str(tmp_path))
    CliRunner().invoke(app, ["on"])
    CliRunner().invoke(app, ["pause", "--duration", "1h"])
    result = CliRunner().invoke(app, ["resume"])
    assert result.exit_code == 0
    state = json.loads((tmp_path / "ambient" / "state.json").read_text())
    assert state["paused_until"] is None


def test_status_does_not_leak_specific_apps(tmp_path, monkeypatch):
    """Hard contract: status output never names specific apps."""
    monkeypatch.setenv("OPENCOMPUTER_PROFILE_HOME", str(tmp_path))
    CliRunner().invoke(app, ["on"])
    result = CliRunner().invoke(app, ["status"])
    # Sanity: no obvious app-name-shaped strings
    forbidden = ["1Password", "Chase", "HDFC", "Robinhood", "Bitwarden", "MyChart"]
    for f in forbidden:
        assert f not in result.output, f"status output leaked app name: {f}"


def test_status_when_enabled_shows_no_heartbeat_when_daemon_not_running(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_PROFILE_HOME", str(tmp_path))
    CliRunner().invoke(app, ["on"])
    result = CliRunner().invoke(app, ["status"])
    assert result.exit_code == 0
    # No heartbeat file → status mentions never/none/missing
    out = result.output.lower()
    assert "heartbeat" in out
    assert "never" in out or "none" in out or "missing" in out


@pytest.mark.parametrize("dur,expected_secs_min", [
    ("30s", 25),
    ("5m", 295),
    ("1h", 3590),
    ("2d", 2 * 86400 - 10),
])
def test_pause_duration_parsing(tmp_path, monkeypatch, dur, expected_secs_min):
    monkeypatch.setenv("OPENCOMPUTER_PROFILE_HOME", str(tmp_path))
    CliRunner().invoke(app, ["on"])
    before = time.time()
    result = CliRunner().invoke(app, ["pause", "--duration", dur])
    assert result.exit_code == 0
    state = json.loads((tmp_path / "ambient" / "state.json").read_text())
    assert state["paused_until"] - before >= expected_secs_min


def test_pause_invalid_duration_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_PROFILE_HOME", str(tmp_path))
    CliRunner().invoke(app, ["on"])
    result = CliRunner().invoke(app, ["pause", "--duration", "abracadabra"])
    assert result.exit_code != 0

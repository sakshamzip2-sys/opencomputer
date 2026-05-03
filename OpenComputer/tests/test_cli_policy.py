"""P2-11: oc policy show / enable / disable / status."""
from __future__ import annotations

from typer.testing import CliRunner

from opencomputer.cli import app


runner = CliRunner()


def test_policy_status_smoke(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    result = runner.invoke(app, ["policy", "status"])
    assert result.exit_code == 0, result.stdout + result.stderr
    out = result.stdout.lower()
    assert "policy engine" in out
    assert "enabled" in out


def test_policy_disable_then_enable(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))

    result = runner.invoke(app, ["policy", "disable"])
    assert result.exit_code == 0
    assert "False" in result.stdout

    result = runner.invoke(app, ["policy", "status"])
    assert "False" in result.stdout

    result = runner.invoke(app, ["policy", "enable"])
    assert "True" in result.stdout

    result = runner.invoke(app, ["policy", "status"])
    assert "True" in result.stdout


def test_policy_show_smoke(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    result = runner.invoke(app, ["policy", "show", "--days", "1"])
    assert result.exit_code == 0
    # No changes yet → empty-state message
    assert "no policy changes" in result.stdout.lower()

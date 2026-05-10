"""Tests for the heartbeat lane (A2 from 2026-05-06 OpenClaw deep-comparison)."""

from __future__ import annotations

from typer.testing import CliRunner


def test_heartbeat_disabled_by_default(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path / "home"))
    from opencomputer.heartbeat import heartbeat_status, is_heartbeat_enabled

    assert is_heartbeat_enabled() is False
    s = heartbeat_status()
    assert s["enabled"] is False
    assert s["lane"] == "heartbeat"


def test_enable_disable_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path / "home"))
    from opencomputer.heartbeat import (
        disable_heartbeat,
        enable_heartbeat,
        heartbeat_status,
        is_heartbeat_enabled,
    )

    job = enable_heartbeat(interval_minutes=15)
    assert job["lane"] == "heartbeat"
    assert is_heartbeat_enabled() is True
    s = heartbeat_status()
    assert s["enabled"] is True
    assert s["interval_minutes"] == 15

    n = disable_heartbeat()
    assert n == 1
    assert is_heartbeat_enabled() is False


def test_enable_idempotent(monkeypatch, tmp_path):
    """Enabling twice doesn't create two jobs."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path / "home"))
    from opencomputer.heartbeat import enable_heartbeat, heartbeat_status

    enable_heartbeat(interval_minutes=30)
    enable_heartbeat(interval_minutes=30)

    from opencomputer.cron import list_jobs

    heartbeats = [j for j in list_jobs() if j.get("lane") == "heartbeat"]
    assert len(heartbeats) == 1


def test_invalid_interval_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path / "home"))
    import pytest

    from opencomputer.heartbeat import enable_heartbeat

    with pytest.raises(ValueError):
        enable_heartbeat(interval_minutes=0)


def test_cli_status_when_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path / "home"))
    from opencomputer.cli_heartbeat import heartbeat_app

    runner = CliRunner()
    result = runner.invoke(heartbeat_app, ["status"])
    assert result.exit_code == 0, result.output
    assert "not enabled" in result.output


def test_cli_enable_then_status(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path / "home"))
    from opencomputer.cli_heartbeat import heartbeat_app

    runner = CliRunner()
    result = runner.invoke(heartbeat_app, ["enable", "--interval", "10"])
    assert result.exit_code == 0, result.output
    assert "heartbeat enabled" in result.output

    result = runner.invoke(heartbeat_app, ["status"])
    assert result.exit_code == 0, result.output
    assert "ENABLED" in result.output


def test_cli_pause_resume(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path / "home"))
    from opencomputer.cli_heartbeat import heartbeat_app

    runner = CliRunner()
    runner.invoke(heartbeat_app, ["enable", "--interval", "10"])
    result = runner.invoke(heartbeat_app, ["pause"])
    assert result.exit_code == 0
    assert "heartbeat paused" in result.output
    result = runner.invoke(heartbeat_app, ["resume"])
    assert result.exit_code == 0
    assert "heartbeat resumed" in result.output


def test_cli_disable_when_not_enabled(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path / "home"))
    from opencomputer.cli_heartbeat import heartbeat_app

    runner = CliRunner()
    result = runner.invoke(heartbeat_app, ["disable"])
    assert result.exit_code == 0
    assert "not enabled" in result.output

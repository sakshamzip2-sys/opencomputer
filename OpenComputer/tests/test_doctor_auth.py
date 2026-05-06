"""Tests for `oc doctor --auth` — credential-pool health surface (Phase 3 / A3 leftover)."""

from __future__ import annotations

from typer.testing import CliRunner


def test_doctor_auth_runs_without_env_vars(monkeypatch):
    """No provider env vars set → exit 0, table shows 'not configured' for each."""
    from opencomputer.cli import app

    # Wipe relevant vars so the test is deterministic.
    for var in (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "ANTHROPIC_KEYS",
        "OPENAI_KEYS",
        "OPENROUTER_KEYS",
    ):
        monkeypatch.delenv(var, raising=False)

    runner = CliRunner()
    result = runner.invoke(app, ["doctor", "--auth"])
    assert result.exit_code == 0, result.output
    assert "Doctor (--auth)" in result.output
    assert "not configured" in result.output


def test_doctor_auth_counts_single_key(monkeypatch):
    from opencomputer.cli import app

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.delenv("ANTHROPIC_KEYS", raising=False)

    runner = CliRunner()
    result = runner.invoke(app, ["doctor", "--auth"])
    assert result.exit_code == 0, result.output
    assert "anthropic" in result.output
    assert "1 single var" in result.output


def test_doctor_auth_counts_pool_keys(monkeypatch):
    """ANTHROPIC_KEYS=k1,k2,k3 → counted as 3."""
    from opencomputer.cli import app

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_KEYS", "sk-ant-1,sk-ant-2,sk-ant-3")

    runner = CliRunner()
    result = runner.invoke(app, ["doctor", "--auth"])
    assert result.exit_code == 0, result.output
    assert "3 from *_KEYS" in result.output


def test_doctor_auth_does_not_leak_secret_values(monkeypatch):
    """Output must never contain the raw key values."""
    from opencomputer.cli import app

    secret = "sk-this-must-never-appear"
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)

    runner = CliRunner()
    result = runner.invoke(app, ["doctor", "--auth"])
    assert result.exit_code == 0, result.output
    assert secret not in result.output

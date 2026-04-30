"""Tests for ``oc login`` / ``oc logout`` (2026-04-30, Hermes-parity Tier S)."""
from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from opencomputer.cli import app
from opencomputer.cli_login import (
    PROVIDER_ENV_MAP,
    _remove_env_var,
    _upsert_env_var,
)


def test_provider_env_map_includes_core_providers():
    for name in ("anthropic", "openai", "groq", "openrouter", "google"):
        assert name in PROVIDER_ENV_MAP


def test_upsert_env_var_creates_file_when_missing(tmp_path):
    env = tmp_path / ".env"
    _upsert_env_var(env, "ANTHROPIC_API_KEY", "sk-ant-123")
    assert env.exists()
    body = env.read_text(encoding="utf-8")
    assert "ANTHROPIC_API_KEY=sk-ant-123" in body


def test_upsert_env_var_preserves_other_lines(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# comment\nFOO=bar\nOPENAI_API_KEY=sk-old\n", encoding="utf-8")
    _upsert_env_var(env, "OPENAI_API_KEY", "sk-new")
    body = env.read_text(encoding="utf-8")
    assert "# comment" in body
    assert "FOO=bar" in body
    assert "OPENAI_API_KEY=sk-new" in body
    assert "OPENAI_API_KEY=sk-old" not in body


def test_upsert_env_var_replaces_existing(tmp_path):
    env = tmp_path / ".env"
    env.write_text("OPENAI_API_KEY=old\n", encoding="utf-8")
    _upsert_env_var(env, "OPENAI_API_KEY", "new")
    body = env.read_text(encoding="utf-8")
    assert body.count("OPENAI_API_KEY=") == 1
    assert "OPENAI_API_KEY=new" in body


def test_upsert_env_var_handles_export_prefix(tmp_path):
    env = tmp_path / ".env"
    env.write_text("export OPENAI_API_KEY=old\n", encoding="utf-8")
    _upsert_env_var(env, "OPENAI_API_KEY", "new")
    body = env.read_text(encoding="utf-8")
    # Replaced — no more `export OPENAI_API_KEY=old`
    assert "export OPENAI_API_KEY=old" not in body
    assert "OPENAI_API_KEY=new" in body


def test_upsert_env_var_sets_0600_permissions(tmp_path):
    env = tmp_path / ".env"
    _upsert_env_var(env, "ANTHROPIC_API_KEY", "sk-ant-123")
    mode = env.stat().st_mode & 0o777
    assert mode == 0o600


def test_remove_env_var_returns_true_when_removed(tmp_path):
    env = tmp_path / ".env"
    env.write_text("FOO=bar\nOPENAI_API_KEY=sk-old\nBAZ=qux\n", encoding="utf-8")
    assert _remove_env_var(env, "OPENAI_API_KEY") is True
    body = env.read_text(encoding="utf-8")
    assert "OPENAI_API_KEY" not in body
    assert "FOO=bar" in body
    assert "BAZ=qux" in body


def test_remove_env_var_returns_false_when_absent(tmp_path):
    env = tmp_path / ".env"
    env.write_text("FOO=bar\n", encoding="utf-8")
    assert _remove_env_var(env, "OPENAI_API_KEY") is False


def test_remove_env_var_returns_false_when_file_missing(tmp_path):
    env = tmp_path / "nonexistent.env"
    assert _remove_env_var(env, "ANTHROPIC_API_KEY") is False


def test_login_rejects_unknown_provider(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(app, ["login", "bogus_provider_xyz"])
    assert result.exit_code == 2
    out = result.stdout + (result.stderr or "")
    assert "Unknown provider" in out or "Unknown" in out


def test_login_writes_env_file(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(app, ["login", "anthropic"], input="sk-ant-test-key\n")
    assert result.exit_code == 0, result.stdout
    env_file = tmp_path / ".env"
    assert env_file.exists()
    body = env_file.read_text(encoding="utf-8")
    assert "ANTHROPIC_API_KEY=sk-ant-test-key" in body


def test_login_rejects_empty_key(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(app, ["login", "anthropic"], input="\n")
    assert result.exit_code == 1


def test_logout_clears_env_var(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    env = tmp_path / ".env"
    env.write_text(
        "ANTHROPIC_API_KEY=sk-ant-old\nFOO=bar\n",
        encoding="utf-8",
    )
    runner = CliRunner()
    result = runner.invoke(app, ["logout", "anthropic"])
    assert result.exit_code == 0, result.stdout
    body = env.read_text(encoding="utf-8")
    assert "ANTHROPIC_API_KEY" not in body
    assert "FOO=bar" in body


def test_logout_rejects_unknown_provider(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(app, ["logout", "bogus_xyz"])
    assert result.exit_code == 2


def test_logout_handles_missing_env_file(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(app, ["logout", "openai"])
    assert result.exit_code == 0
    out = result.stdout + (result.stderr or "")
    assert "no credentials" in out.lower() or "not stored" in out.lower()


def test_login_logout_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    runner = CliRunner()
    r1 = runner.invoke(app, ["login", "openai"], input="sk-test\n")
    assert r1.exit_code == 0
    r2 = runner.invoke(app, ["logout", "openai"])
    assert r2.exit_code == 0
    body = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "OPENAI_API_KEY" not in body

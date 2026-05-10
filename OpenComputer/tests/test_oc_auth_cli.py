"""T8 — `oc auth` CLI subcommand group."""

from __future__ import annotations

import pytest
import yaml
from typer.testing import CliRunner


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def auth_app(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    import importlib

    import opencomputer.cli_auth as mod
    importlib.reload(mod)
    return mod.auth_app


def _read_cfg(tmp_path) -> dict:
    cfg_path = tmp_path / "config.yaml"
    if not cfg_path.exists():
        return {}
    return yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}


def test_list_empty_pool(runner, auth_app):
    result = runner.invoke(auth_app, ["list"])
    assert result.exit_code == 0
    assert "no credential" in result.stdout.lower() or "empty" in result.stdout.lower()


def test_add_with_inline_key(runner, auth_app, tmp_path):
    result = runner.invoke(
        auth_app, ["add", "openrouter", "--key", "sk-or-v1-aaa"]
    )
    assert result.exit_code == 0
    cfg = _read_cfg(tmp_path)
    assert cfg["credential_pools"]["openrouter"] == ["sk-or-v1-aaa"]


def test_add_with_key_env(runner, auth_app, tmp_path):
    result = runner.invoke(
        auth_app, ["add", "openrouter", "--key-env", "MY_OR_KEY"]
    )
    assert result.exit_code == 0
    cfg = _read_cfg(tmp_path)
    # Stored as ${ENV_NAME} indirection (matches Hermes auth.json format).
    assert cfg["credential_pools"]["openrouter"] == ["${MY_OR_KEY}"]


def test_add_no_key_or_env_errors(runner, auth_app):
    result = runner.invoke(auth_app, ["add", "openrouter"])
    assert result.exit_code != 0


def test_add_appends_multiple(runner, auth_app, tmp_path):
    runner.invoke(auth_app, ["add", "openrouter", "--key", "sk-1"])
    runner.invoke(auth_app, ["add", "openrouter", "--key", "sk-2"])
    cfg = _read_cfg(tmp_path)
    assert cfg["credential_pools"]["openrouter"] == ["sk-1", "sk-2"]


def test_list_after_add_shows_masked(runner, auth_app, tmp_path):
    runner.invoke(auth_app, ["add", "openrouter", "--key", "sk-or-v1-aaa"])
    result = runner.invoke(auth_app, ["list"])
    assert result.exit_code == 0
    assert "openrouter" in result.stdout
    # Masked — full key NOT in output.
    assert "sk-or-v1-aaa" not in result.stdout


def test_list_filtered_by_provider(runner, auth_app):
    runner.invoke(auth_app, ["add", "openrouter", "--key", "sk-or"])
    runner.invoke(auth_app, ["add", "anthropic", "--key", "sk-an"])
    result = runner.invoke(auth_app, ["list", "openrouter"])
    assert result.exit_code == 0
    assert "openrouter" in result.stdout
    assert "anthropic" not in result.stdout


def test_remove_by_index(runner, auth_app, tmp_path):
    runner.invoke(auth_app, ["add", "openrouter", "--key", "sk-1"])
    runner.invoke(auth_app, ["add", "openrouter", "--key", "sk-2"])
    result = runner.invoke(auth_app, ["remove", "openrouter", "0"])
    assert result.exit_code == 0
    cfg = _read_cfg(tmp_path)
    assert cfg["credential_pools"]["openrouter"] == ["sk-2"]


def test_remove_invalid_index_errors(runner, auth_app):
    runner.invoke(auth_app, ["add", "openrouter", "--key", "sk-1"])
    result = runner.invoke(auth_app, ["remove", "openrouter", "99"])
    assert result.exit_code != 0


def test_remove_unknown_provider_errors(runner, auth_app):
    result = runner.invoke(auth_app, ["remove", "ghost", "0"])
    assert result.exit_code != 0


def test_reset_writes_force_reset_marker(runner, auth_app, tmp_path):
    runner.invoke(auth_app, ["add", "openrouter", "--key", "sk-1"])
    result = runner.invoke(auth_app, ["reset", "openrouter"])
    assert result.exit_code == 0
    cfg = _read_cfg(tmp_path)
    assert "credential_pool_reset_at" in cfg
    assert "openrouter" in cfg["credential_pool_reset_at"]

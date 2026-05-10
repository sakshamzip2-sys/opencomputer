"""Tests for `oc config set` secret-routing.

Heuristic: keys matching API_KEY|TOKEN|SECRET|PASSWORD|WEBHOOK_URL pattern
go to .env. Everything else goes to config.yaml. Override with
--secret/--public.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from opencomputer.profile_env_init import is_secret_key, write_env_var


runner = CliRunner()


def test_is_secret_key_recognizes_api_key() -> None:
    assert is_secret_key("OPENAI_API_KEY")
    assert is_secret_key("openai_api_key")
    assert is_secret_key("custom.api_key")


def test_is_secret_key_recognizes_token() -> None:
    assert is_secret_key("GITHUB_TOKEN")
    assert is_secret_key("github_token")


def test_is_secret_key_recognizes_password() -> None:
    assert is_secret_key("DB_PASSWORD")


def test_is_secret_key_recognizes_secret() -> None:
    assert is_secret_key("CLIENT_SECRET")
    assert is_secret_key("APP_SECRET")


def test_is_secret_key_recognizes_webhook_url() -> None:
    assert is_secret_key("SLACK_WEBHOOK_URL")
    assert is_secret_key("DISCORD_WEBHOOK_URL")


def test_is_secret_key_rejects_non_secret_keys() -> None:
    assert not is_secret_key("memory.provider")
    assert not is_secret_key("max_iterations")
    assert not is_secret_key("language")
    assert not is_secret_key("model.provider")


def test_write_env_var_creates_file_with_0600(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    write_env_var(env_path, "TEST_KEY", "abc")
    assert env_path.exists()
    assert "TEST_KEY=abc" in env_path.read_text()
    mode = env_path.stat().st_mode & 0o777
    assert mode == 0o600


def test_write_env_var_appends_new_key(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("EXISTING=v\n")
    write_env_var(env_path, "TEST_KEY", "abc")
    contents = env_path.read_text()
    assert "EXISTING=v" in contents
    assert "TEST_KEY=abc" in contents


def test_write_env_var_updates_existing_key(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("FOO=old_value\nBAR=other\n")
    write_env_var(env_path, "FOO", "new_value")
    contents = env_path.read_text()
    assert "FOO=new_value" in contents
    assert "FOO=old_value" not in contents
    assert "BAR=other" in contents


def test_write_env_var_quotes_values_with_spaces(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    write_env_var(env_path, "MULTI", "hello world")
    body = env_path.read_text()
    assert 'MULTI="hello world"' in body


def test_cli_config_set_routes_api_key_to_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`oc config set OPENAI_API_KEY ...` writes to .env."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    from opencomputer.cli import app

    result = runner.invoke(app, ["config", "set", "OPENAI_API_KEY", "sk-test-secret"])
    assert result.exit_code == 0, result.output
    # The output mentions the .env routing.
    assert ".env" in result.output
    env_file = tmp_path / ".env"
    assert env_file.exists()
    assert "OPENAI_API_KEY=sk-test-secret" in env_file.read_text()


def test_cli_config_set_routes_non_secret_to_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`oc config set memory.provider ...` writes to config.yaml."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    from opencomputer.cli import app

    result = runner.invoke(app, ["config", "set", "memory.provider", "honcho"])
    assert result.exit_code == 0, result.output
    yaml_file = tmp_path / "config.yaml"
    assert yaml_file.exists()
    assert "honcho" in yaml_file.read_text()


def test_cli_config_set_secret_flag_forces_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--secret` forces .env even for a non-secret-looking key."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    from opencomputer.cli import app

    result = runner.invoke(
        app, ["config", "set", "--secret", "MY_CUSTOM_VALUE", "foo"]
    )
    assert result.exit_code == 0, result.output
    env_file = tmp_path / ".env"
    assert env_file.exists()
    assert "MY_CUSTOM_VALUE=foo" in env_file.read_text()


def test_cli_config_set_public_flag_forces_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--public` forces config.yaml even for a secret-looking key, with warning.

    Uses ``loop.delegation.api_key`` — a valid dotted path that ends in
    ``api_key`` and matches the secret heuristic.
    """
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    from opencomputer.cli import app

    result = runner.invoke(
        app, ["config", "set", "--public", "loop.delegation.api_key", "fake-key"]
    )
    assert result.exit_code == 0, result.output
    yaml_file = tmp_path / "config.yaml"
    assert yaml_file.exists()
    # Warning should be visible.
    assert "warning" in result.output.lower() or "WARN" in result.output

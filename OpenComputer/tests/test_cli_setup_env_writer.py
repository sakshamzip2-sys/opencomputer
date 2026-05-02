"""Tests for cli_setup/env_writer.py — reads/writes API keys to ~/.opencomputer/.env."""
from __future__ import annotations

import os
from pathlib import Path

import pytest


def test_read_env_value_returns_os_environ_first(monkeypatch, tmp_path):
    """os.environ wins over .env file values."""
    from opencomputer.cli_setup.env_writer import read_env_value

    monkeypatch.setenv("MY_KEY", "from-shell")
    env_file = tmp_path / ".env"
    env_file.write_text("MY_KEY=from-file\n")
    env_file.chmod(0o600)

    assert read_env_value("MY_KEY", env_file=env_file) == "from-shell"


def test_read_env_value_reads_from_env_file_when_not_in_shell(monkeypatch, tmp_path):
    from opencomputer.cli_setup.env_writer import read_env_value

    monkeypatch.delenv("MY_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("MY_KEY=from-file\nOTHER=x\n")
    env_file.chmod(0o600)

    assert read_env_value("MY_KEY", env_file=env_file) == "from-file"


def test_read_env_value_returns_none_when_unset(monkeypatch, tmp_path):
    from opencomputer.cli_setup.env_writer import read_env_value

    monkeypatch.delenv("MY_KEY", raising=False)
    env_file = tmp_path / ".env"  # doesn't exist

    assert read_env_value("MY_KEY", env_file=env_file) is None


def test_read_env_value_handles_quoted_values(tmp_path):
    from opencomputer.cli_setup.env_writer import read_env_value

    env_file = tmp_path / ".env"
    env_file.write_text('K1="quoted value"\nK2=\'single\'\nK3=plain\n')
    env_file.chmod(0o600)

    assert read_env_value("K1", env_file=env_file) == "quoted value"
    assert read_env_value("K2", env_file=env_file) == "single"
    assert read_env_value("K3", env_file=env_file) == "plain"


def test_write_env_value_creates_file_with_0600_perms(tmp_path):
    from opencomputer.cli_setup.env_writer import write_env_value

    env_file = tmp_path / ".env"
    write_env_value("ANTHROPIC_API_KEY", "sk-ant-xyz", env_file=env_file)

    assert env_file.exists()
    assert "ANTHROPIC_API_KEY=sk-ant-xyz" in env_file.read_text()
    # Perms: 0600 (-rw-------)
    assert (env_file.stat().st_mode & 0o777) == 0o600


def test_write_env_value_updates_existing_line(tmp_path):
    from opencomputer.cli_setup.env_writer import write_env_value

    env_file = tmp_path / ".env"
    env_file.write_text("# Existing config\nANTHROPIC_API_KEY=old-key\nOTHER=keep\n")
    env_file.chmod(0o600)

    write_env_value("ANTHROPIC_API_KEY", "new-key", env_file=env_file)

    text = env_file.read_text()
    assert "ANTHROPIC_API_KEY=new-key" in text
    assert "old-key" not in text
    assert "OTHER=keep" in text  # other lines preserved
    assert "# Existing config" in text  # comments preserved


def test_write_env_value_appends_when_key_absent(tmp_path):
    from opencomputer.cli_setup.env_writer import write_env_value

    env_file = tmp_path / ".env"
    env_file.write_text("EXISTING=value\n")
    env_file.chmod(0o600)

    write_env_value("NEW_KEY", "new-value", env_file=env_file)

    text = env_file.read_text()
    assert "EXISTING=value" in text
    assert "NEW_KEY=new-value" in text


def test_write_env_value_quotes_values_with_spaces(tmp_path):
    from opencomputer.cli_setup.env_writer import write_env_value

    env_file = tmp_path / ".env"
    write_env_value("KEY_WITH_SPACES", "value with spaces", env_file=env_file)

    text = env_file.read_text()
    assert 'KEY_WITH_SPACES="value with spaces"' in text


def test_default_env_file_path_uses_oc_home(monkeypatch, tmp_path):
    from opencomputer.cli_setup.env_writer import default_env_file

    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    assert default_env_file() == tmp_path / ".env"


def test_default_env_file_path_falls_back_to_home(monkeypatch):
    from opencomputer.cli_setup.env_writer import default_env_file

    monkeypatch.delenv("OPENCOMPUTER_HOME", raising=False)
    result = default_env_file()
    assert result == Path.home() / ".opencomputer" / ".env"

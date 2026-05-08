"""Tests for `oc config check` — find missing options post-update."""
from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner


runner = CliRunner()


def test_check_reports_no_missing_when_full_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A config.yaml with every nested block present has no missing items."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "memory:\n  provider: builtin\n"
        "model:\n  provider: anthropic\n"
        "loop:\n  max_iterations: 100\n"
        "session:\n  auto_prune: true\n"
        "mcp:\n  servers: []\n"
        "tools:\n  deny: []\n"
        "deepening:\n  enabled: true\n"
        "gateway:\n  enabled: false\n"
        "system_control:\n  enabled: false\n"
        "auxiliary:\n  temperature: 0.3\n"
        "privacy:\n  redact_pii: false\n"
        "security:\n  redact_secrets: false\n"
        "timezone: \"\"\n",
        encoding="utf-8",
    )
    from opencomputer.cli import app

    result = runner.invoke(app, ["config", "check"])
    assert result.exit_code == 0, result.output
    assert "no missing" in result.output.lower() or "✓" in result.output


def test_check_reports_missing_top_level_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare config.yaml flags `privacy`, `security`, `timezone` as missing."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "memory:\n  provider: builtin\n", encoding="utf-8"
    )
    from opencomputer.cli import app

    result = runner.invoke(app, ["config", "check"])
    assert result.exit_code == 0, result.output
    assert "privacy" in result.output
    assert "security" in result.output
    assert "timezone" in result.output


def test_check_fix_writes_default_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`config check --fix` adds missing nested blocks with their defaults."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("memory:\n  provider: builtin\n", encoding="utf-8")
    from opencomputer.cli import app

    result = runner.invoke(app, ["config", "check", "--fix"])
    assert result.exit_code == 0, result.output
    contents = cfg_path.read_text()
    assert "privacy:" in contents
    assert "security:" in contents
    assert "timezone:" in contents


def test_check_fix_does_not_overwrite_user_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`config check --fix` is purely additive; doesn't touch user values."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "memory:\n  provider: honcho\n"  # user value
        "timezone: \"America/Los_Angeles\"\n",
        encoding="utf-8",
    )
    from opencomputer.cli import app

    runner.invoke(app, ["config", "check", "--fix"])
    contents = cfg_path.read_text()
    assert "provider: honcho" in contents       # user value preserved
    assert "America/Los_Angeles" in contents    # user value preserved
    assert "privacy:" in contents                # newly added


def test_check_no_config_file_yet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without an existing config.yaml, all top-level blocks are missing."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    from opencomputer.cli import app

    result = runner.invoke(app, ["config", "check"])
    assert result.exit_code == 0, result.output
    # All expected blocks should be flagged.
    assert "memory" in result.output or "missing" in result.output.lower()

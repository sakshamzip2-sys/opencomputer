"""Tests for ``opencomputer insights cost`` (Hermes B4 follow-up)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from opencomputer.agent.state import SessionDB
from opencomputer.cli_insights import insights_app


@pytest.fixture
def runner() -> CliRunner:
    # Wide terminal so Rich doesn't truncate model names like "claude-opus-4-7"
    return CliRunner(env={"COLUMNS": "200"})


@pytest.fixture
def populated_db(tmp_path: Path) -> Path:
    """Create an llm_calls-populated SessionDB and return its parent dir."""
    profile_home = tmp_path / "profile"
    profile_home.mkdir()
    db_path = profile_home / "sessions.db"
    db = SessionDB(db_path)
    db.ensure_session("s-1", platform="cli")
    db.record_llm_call(
        session_id="s-1",
        provider="anthropic",
        model="claude-opus-4-7",
        input_tokens=1000,
        output_tokens=500,
        cost_usd=0.05,
    )
    db.record_llm_call(
        session_id="s-1",
        provider="openai",
        model="gpt-4o",
        input_tokens=2000,
        output_tokens=300,
        cost_usd=0.01,
    )
    db.record_llm_call(
        session_id="s-1",
        provider="local",
        model="custom-model",
        input_tokens=50,
        output_tokens=20,
        cost_usd=None,  # pricing unknown
    )
    return profile_home


def _patched_default_config(profile_home: Path):
    """Return a fake default_config that points at our temp profile dir."""
    class _C:
        home = profile_home

    return lambda: _C()


def test_cost_subcommand_shows_table(
    runner: CliRunner, populated_db: Path
) -> None:
    with patch(
        "opencomputer.cli_insights.default_config",
        _patched_default_config(populated_db),
    ):
        result = runner.invoke(insights_app, ["cost", "--days", "30"])
    assert result.exit_code == 0
    out = result.stdout
    assert "LLM cost by model" in out
    assert "claude-opus-4-7" in out
    assert "gpt-4o" in out
    assert "custom-model" in out
    # Known cost rows show $0.05 and $0.01
    assert "$0.0500" in out
    assert "$0.0100" in out
    # Unknown pricing row renders em-dash
    assert "no pricing data" in out


def test_cost_subcommand_no_db(
    runner: CliRunner, tmp_path: Path
) -> None:
    """When sessions.db doesn't exist yet, friendly message + exit 0."""
    with patch(
        "opencomputer.cli_insights.default_config",
        _patched_default_config(tmp_path / "absent"),
    ):
        result = runner.invoke(insights_app, ["cost"])
    assert result.exit_code == 0
    assert "No sessions.db yet" in result.stdout


def test_cost_subcommand_empty_window(
    runner: CliRunner, tmp_path: Path
) -> None:
    """Empty llm_calls table → friendly message, no crash."""
    profile_home = tmp_path / "profile2"
    profile_home.mkdir()
    SessionDB(profile_home / "sessions.db")  # init schema, no rows
    with patch(
        "opencomputer.cli_insights.default_config",
        _patched_default_config(profile_home),
    ):
        result = runner.invoke(insights_app, ["cost"])
    assert result.exit_code == 0
    assert "No llm_calls rows" in result.stdout


def test_cost_subcommand_groups_by_provider(
    runner: CliRunner, populated_db: Path
) -> None:
    with patch(
        "opencomputer.cli_insights.default_config",
        _patched_default_config(populated_db),
    ):
        result = runner.invoke(insights_app, ["cost", "--by", "provider"])
    assert result.exit_code == 0
    assert "LLM cost by provider" in result.stdout
    assert "anthropic" in result.stdout
    assert "openai" in result.stdout
    assert "local" in result.stdout


def test_cost_subcommand_total_line(
    runner: CliRunner, populated_db: Path
) -> None:
    with patch(
        "opencomputer.cli_insights.default_config",
        _patched_default_config(populated_db),
    ):
        result = runner.invoke(insights_app, ["cost"])
    assert result.exit_code == 0
    # 0.05 + 0.01 = 0.06
    assert "Total: $0.0600" in result.stdout
    # Warn that local model is missing pricing
    assert "lower bound" in result.stdout

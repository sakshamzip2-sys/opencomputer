"""Plan 3 Tasks 3 + 5 — oc profile analyze CLI tests."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from opencomputer.cli_profile_analyze import profile_analyze_app


def test_analyze_run_writes_cache_when_history_present(tmp_path: Path, monkeypatch) -> None:
    """Run reads SessionDB, computes suggestions, writes cache."""
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    from opencomputer.agent.state import SessionDB
    db_dir = tmp_path / "default" / "sessions.db"
    db_dir.parent.mkdir(parents=True, exist_ok=True)
    db = SessionDB(db_dir)
    for i in range(15):
        db.create_session(
            session_id=f"sid-{i}",
            platform="cli",
            model="test",
            cwd="/Users/test/Vscode/work",
        )

    runner = CliRunner()
    result = runner.invoke(profile_analyze_app, ["run"])
    assert result.exit_code == 0
    assert (tmp_path / "profile_analysis_cache.json").exists()


def test_analyze_run_idempotent_on_empty_db(tmp_path: Path, monkeypatch) -> None:
    """No SessionDB → run prints "no analysis" message, exit 0."""
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(profile_analyze_app, ["run"])
    assert result.exit_code == 0
    assert "nothing to analyze" in result.stdout.lower() or "no session" in result.stdout.lower()


def test_analyze_status_no_cache(tmp_path: Path, monkeypatch) -> None:
    """status with no cache → 'never' message + exit 0."""
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(profile_analyze_app, ["status"])
    assert result.exit_code == 0
    assert "never" in result.stdout.lower() or "Last run" in result.stdout


def test_analyze_status_with_cache(tmp_path: Path, monkeypatch) -> None:
    """status with cache → shows last_run timestamp."""
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    from opencomputer.profile_analysis_daily import save_cache
    save_cache(suggestions=[], dismissed=[])
    runner = CliRunner()
    result = runner.invoke(profile_analyze_app, ["status"])
    assert result.exit_code == 0
    assert "Last run:" in result.stdout


@pytest.mark.skipif(
    sys.platform not in ("darwin", "linux"),
    reason="install/uninstall only on macOS or Linux",
)
def test_analyze_install_uninstall_round_trip(tmp_path: Path, monkeypatch) -> None:
    """install → uninstall round-trip works without crashing.

    Doesn't actually load the service (no launchctl/systemctl in test
    environment); just verifies the CLI commands don't error.
    """
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    if sys.platform == "darwin":
        monkeypatch.setattr(
            "opencomputer.service.launchd._launch_agents_dir",
            lambda: tmp_path / "LaunchAgents",
        )
    else:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    runner = CliRunner()
    install_result = runner.invoke(profile_analyze_app, ["install"])
    assert install_result.exit_code == 0
    assert "installed" in install_result.stdout.lower()

    uninstall_result = runner.invoke(profile_analyze_app, ["uninstall"])
    assert uninstall_result.exit_code == 0

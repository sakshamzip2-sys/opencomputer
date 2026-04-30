"""`oc session delete <id>` CLI tests."""
from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from opencomputer.agent.state import SessionDB
from opencomputer.cli_session import session_app
from plugin_sdk.core import Message


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the active profile at a temp dir so we don't trash real data."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    return tmp_path


def _seed(home: Path, sid: str) -> None:
    db = SessionDB(home / "sessions.db")
    db.create_session(sid, platform="cli", model="m", title=f"t-{sid}")
    db.append_messages_batch(sid, [Message(role="user", content="hi")])


def test_delete_with_yes_flag_removes_session(runner: CliRunner, home: Path) -> None:
    _seed(home, "abc123")
    result = runner.invoke(session_app, ["delete", "abc123", "--yes"])
    assert result.exit_code == 0, result.output
    assert SessionDB(home / "sessions.db").get_session("abc123") is None


def test_delete_without_yes_aborts_on_no(runner: CliRunner, home: Path) -> None:
    _seed(home, "abc123")
    result = runner.invoke(session_app, ["delete", "abc123"], input="n\n")
    assert result.exit_code == 1
    assert SessionDB(home / "sessions.db").get_session("abc123") is not None


def test_delete_unknown_id_exits_1(runner: CliRunner, home: Path) -> None:
    result = runner.invoke(session_app, ["delete", "ghost", "--yes"])
    assert result.exit_code == 1
    assert "not found" in result.output.lower()


def test_delete_confirms_with_y_then_removes(runner: CliRunner, home: Path) -> None:
    _seed(home, "abc123")
    result = runner.invoke(session_app, ["delete", "abc123"], input="y\n")
    assert result.exit_code == 0
    assert SessionDB(home / "sessions.db").get_session("abc123") is None

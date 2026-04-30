"""`oc session prune` filter parser + integration tests."""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from opencomputer.agent.state import SessionDB
from opencomputer.cli_session import _parse_age, session_app
from plugin_sdk.core import Message


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    return tmp_path


# ─── _parse_age ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "spec,expected_seconds",
    [
        ("30d", 30 * 86400),
        ("6w", 6 * 7 * 86400),
        ("3mo", 3 * 30 * 86400),
        ("1y", 365 * 86400),
        ("1d", 86400),
    ],
)
def test_parse_age_accepts_suffix_forms(spec: str, expected_seconds: int) -> None:
    assert _parse_age(spec) == expected_seconds


@pytest.mark.parametrize("bad", ["30", "abc", "0d", "-5d", "10x", "", "d", "3.5d"])
def test_parse_age_rejects_bad_input(bad: str) -> None:
    with pytest.raises(ValueError):
        _parse_age(bad)


# ─── prune command ──────────────────────────────────────────────


def _seed_at_age(
    home: Path,
    sid: str,
    *,
    age_days: float,
    title: str = "x",
    messages: int = 3,
) -> None:
    db = SessionDB(home / "sessions.db")
    db.create_session(sid, platform="cli", model="m", title=title)
    db.append_messages_batch(
        sid, [Message(role="user", content="hi") for _ in range(messages)]
    )
    backdated = time.time() - age_days * 86400
    with db._connect() as c:
        c.execute(
            "UPDATE sessions SET started_at = ? WHERE id = ?",
            (backdated, sid),
        )


def test_prune_requires_at_least_one_filter(runner: CliRunner, home: Path) -> None:
    result = runner.invoke(session_app, ["prune", "--yes"])
    assert result.exit_code == 1
    assert "filter" in result.output.lower()


def test_prune_dry_run_makes_no_changes(runner: CliRunner, home: Path) -> None:
    _seed_at_age(home, "old", age_days=60, messages=1)
    result = runner.invoke(
        session_app, ["prune", "--older-than", "30d", "--dry-run"]
    )
    assert result.exit_code == 0
    assert SessionDB(home / "sessions.db").get_session("old") is not None
    assert (
        "would delete" in result.output.lower()
        or "dry-run" in result.output.lower()
    )


def test_prune_older_than_30d(runner: CliRunner, home: Path) -> None:
    _seed_at_age(home, "old", age_days=60, messages=2)
    _seed_at_age(home, "young", age_days=5, messages=2)
    result = runner.invoke(
        session_app, ["prune", "--older-than", "30d", "--yes"]
    )
    assert result.exit_code == 0, result.output
    db = SessionDB(home / "sessions.db")
    assert db.get_session("old") is None
    assert db.get_session("young") is not None


def test_prune_untitled_filter(runner: CliRunner, home: Path) -> None:
    _seed_at_age(home, "untitled", age_days=1, title="", messages=2)
    _seed_at_age(home, "named", age_days=1, title="my-session", messages=2)
    result = runner.invoke(session_app, ["prune", "--untitled", "--yes"])
    assert result.exit_code == 0, result.output
    db = SessionDB(home / "sessions.db")
    assert db.get_session("untitled") is None
    assert db.get_session("named") is not None


def test_prune_empty_filter(runner: CliRunner, home: Path) -> None:
    _seed_at_age(home, "empty", age_days=1, messages=1)
    _seed_at_age(home, "real", age_days=1, messages=10)
    result = runner.invoke(session_app, ["prune", "--empty", "--yes"])
    assert result.exit_code == 0, result.output
    db = SessionDB(home / "sessions.db")
    assert db.get_session("empty") is None
    assert db.get_session("real") is not None


def test_prune_filters_compose_with_and(runner: CliRunner, home: Path) -> None:
    _seed_at_age(home, "untitled-old", age_days=60, title="", messages=1)
    _seed_at_age(home, "untitled-young", age_days=5, title="", messages=1)
    _seed_at_age(home, "named-old", age_days=60, title="keep-me", messages=1)
    result = runner.invoke(
        session_app,
        ["prune", "--untitled", "--older-than", "30d", "--yes"],
    )
    assert result.exit_code == 0, result.output
    db = SessionDB(home / "sessions.db")
    assert db.get_session("untitled-old") is None
    assert db.get_session("untitled-young") is not None
    assert db.get_session("named-old") is not None


def test_prune_invalid_age_format_exits_nonzero(
    runner: CliRunner, home: Path
) -> None:
    result = runner.invoke(
        session_app, ["prune", "--older-than", "abc", "--yes"]
    )
    assert result.exit_code != 0

"""Tests for `opencomputer session` CLI (G.33 / Tier 4).

Covers list, show, fork, resume against a tmp_path-rooted SessionDB.
The CLI uses ``_home()`` to find sessions.db; we monkey-patch
``OPENCOMPUTER_HOME_ROOT`` so each test gets an isolated profile.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from opencomputer.agent.state import SessionDB
from opencomputer.cli_session import session_app
from plugin_sdk.core import Message

runner = CliRunner()


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point _home() at a fresh tmp_path so each test sees an empty DB."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    return tmp_path


def _seed(home: Path, session_id: str, *, msgs: int = 3) -> SessionDB:
    """Drop a session + ``msgs`` messages into the active profile DB."""
    db = SessionDB(home / "sessions.db")
    db.create_session(
        session_id, platform="cli", model="claude-opus-4-7", title="Demo session"
    )
    for i in range(msgs):
        role = "user" if i % 2 == 0 else "assistant"
        db.append_message(
            session_id, Message(role=role, content=f"message {i}")
        )
    return db


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


class TestList:
    def test_empty_profile_shows_hint(self, isolated_home: Path) -> None:
        result = runner.invoke(session_app, ["list"])
        assert result.exit_code == 0
        assert "no sessions" in result.stdout.lower()

    def test_lists_seeded_session(self, isolated_home: Path) -> None:
        _seed(isolated_home, "abc123def456", msgs=2)
        result = runner.invoke(session_app, ["list"])
        assert result.exit_code == 0
        # The id may render with line-break wrapping in Rich's table
        # output, so check character-by-character (drop whitespace +
        # ANSI noise).
        flat = "".join(c for c in result.stdout if c.isalnum())
        assert "abc123def456" in flat

    def test_limit_clamps(self, isolated_home: Path) -> None:
        # Limit above max should fail validation (typer min/max).
        result = runner.invoke(session_app, ["list", "--limit", "5000"])
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


class TestShow:
    def test_unknown_session_returns_error(self, isolated_home: Path) -> None:
        result = runner.invoke(session_app, ["show", "nonexistent"])
        assert result.exit_code == 1
        assert "not found" in result.stdout.lower()

    def test_shows_metadata_and_head_preview(
        self, isolated_home: Path
    ) -> None:
        _seed(isolated_home, "demo-id", msgs=4)
        result = runner.invoke(session_app, ["show", "demo-id"])
        assert result.exit_code == 0
        assert "demo-id" in result.stdout
        assert "Demo session" in result.stdout
        assert "claude-opus-4-7" in result.stdout
        # Default head=5; we seeded 4 messages — all should preview.
        assert "message 0" in result.stdout
        assert "message 3" in result.stdout

    def test_head_zero_skips_preview(self, isolated_home: Path) -> None:
        _seed(isolated_home, "demo-id", msgs=3)
        result = runner.invoke(session_app, ["show", "demo-id", "--head", "0"])
        assert result.exit_code == 0
        assert "Demo session" in result.stdout
        assert "message 0" not in result.stdout


# ---------------------------------------------------------------------------
# fork
# ---------------------------------------------------------------------------


class TestFork:
    def test_unknown_source_returns_error(self, isolated_home: Path) -> None:
        result = runner.invoke(session_app, ["fork", "nonexistent"])
        assert result.exit_code == 1
        assert "not found" in result.stdout.lower()

    def test_fork_clones_session_and_messages(
        self, isolated_home: Path
    ) -> None:
        _seed(isolated_home, "src-id", msgs=4)
        result = runner.invoke(session_app, ["fork", "src-id"])
        assert result.exit_code == 0
        assert "forked" in result.stdout.lower()
        # The new id is a fresh UUID hex — verify by listing.
        db = SessionDB(isolated_home / "sessions.db")
        sessions = db.list_sessions(limit=10)
        ids = [s["id"] for s in sessions]
        # Both source + fork should be present.
        assert "src-id" in ids
        new_ids = [i for i in ids if i != "src-id"]
        assert len(new_ids) == 1
        # The forked session should have all 4 messages.
        forked = new_ids[0]
        msgs = db.get_messages(forked)
        assert len(msgs) == 4
        assert msgs[0].content == "message 0"
        assert msgs[3].content == "message 3"

    def test_fork_uses_explicit_title(self, isolated_home: Path) -> None:
        _seed(isolated_home, "src-id", msgs=1)
        runner.invoke(
            session_app,
            ["fork", "src-id", "--title", "what-if branch"],
        )
        db = SessionDB(isolated_home / "sessions.db")
        sessions = db.list_sessions(limit=10)
        forked = next(s for s in sessions if s["id"] != "src-id")
        assert forked["title"] == "what-if branch"

    def test_fork_default_title_appends_suffix(
        self, isolated_home: Path
    ) -> None:
        _seed(isolated_home, "src-id", msgs=1)
        runner.invoke(session_app, ["fork", "src-id"])
        db = SessionDB(isolated_home / "sessions.db")
        sessions = db.list_sessions(limit=10)
        forked = next(s for s in sessions if s["id"] != "src-id")
        assert "(fork)" in (forked["title"] or "")


# ---------------------------------------------------------------------------
# resume
# ---------------------------------------------------------------------------


class TestResume:
    def test_unknown_session_returns_error(self, isolated_home: Path) -> None:
        result = runner.invoke(session_app, ["resume", "nonexistent"])
        assert result.exit_code == 1
        assert "not found" in result.stdout.lower()

    def test_prints_chat_resume_command(self, isolated_home: Path) -> None:
        _seed(isolated_home, "abc-resumable", msgs=2)
        result = runner.invoke(session_app, ["resume", "abc-resumable"])
        assert result.exit_code == 0
        # The exact resume command should be visible (modulo Rich
        # wrapping). Check the pieces individually.
        assert "opencomputer chat" in result.stdout
        assert "--resume" in result.stdout
        assert "abc-resumable" in result.stdout

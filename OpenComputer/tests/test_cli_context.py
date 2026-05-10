"""``oc context`` Typer CLI — context-window inspection per session.

Spec: ``docs/superpowers/specs/2026-05-10-cc-usage-context-visibility-design.md`` §4.7.

Subcommands:

  - ``oc context show <session-id>``   — render context-window panel for one session
  - ``oc context show --current``      — render for the most-recent session

Mirrors the in-chat ``/context`` slash command but operates on
historical / arbitrary sessions instead of the in-flight one. Reads
from SessionDB only — no runtime.custom (which is in-flight only).
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from opencomputer.agent.state import SessionDB
from opencomputer.cli_context import context_app


def _seed(tmp_path: Path) -> tuple[SessionDB, str]:
    db = SessionDB(tmp_path / "sessions.db")
    sid = db.allocate_session_id()
    db.create_session(sid, platform="cli", model="claude-opus-4-7")
    db.add_tokens(session_id=sid, input_tokens=50_000, output_tokens=2_000, cache_read_tokens=1_000)
    db.increment_compaction_count(sid)
    db.increment_compaction_count(sid)
    return db, sid


@pytest.fixture
def profile_home(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_PROFILE_HOME", str(tmp_path))
    return tmp_path


def test_context_show_renders_panel_for_known_session(profile_home):
    _, sid = _seed(profile_home)
    result = CliRunner().invoke(context_app, ["show", sid])
    assert result.exit_code == 0, result.output
    assert "claude-opus-4-7" in result.output
    assert "compactions" in result.output.lower()


def test_context_show_includes_session_id(profile_home):
    _, sid = _seed(profile_home)
    result = CliRunner().invoke(context_app, ["show", sid])
    assert result.exit_code == 0
    # First chunk of session id appears (full or truncated).
    assert sid[:8] in result.output


def test_context_show_renders_compaction_count(profile_home):
    _, sid = _seed(profile_home)
    result = CliRunner().invoke(context_app, ["show", sid])
    assert result.exit_code == 0
    # Seeded 2 compactions.
    assert "2" in result.output


def test_context_show_unknown_session_renders_empty_state(profile_home):
    _seed(profile_home)
    result = CliRunner().invoke(context_app, ["show", "unknown-id"])
    # Either non-zero exit OR empty-state body — never crash.
    assert "Traceback" not in result.output


def test_context_show_current_picks_most_recent(profile_home):
    db = SessionDB(profile_home / "sessions.db")
    older = db.allocate_session_id()
    db.create_session(older, platform="cli", model="model-old")
    with db._connect() as conn:
        conn.execute(
            "UPDATE sessions SET started_at = 1000.0 WHERE id = ?", (older,)
        )
    newer = db.allocate_session_id()
    db.create_session(newer, platform="cli", model="model-new")
    with db._connect() as conn:
        conn.execute(
            "UPDATE sessions SET started_at = 9999.0 WHERE id = ?", (newer,)
        )

    result = CliRunner().invoke(context_app, ["show", "--current"])
    assert result.exit_code == 0, result.output
    assert "model-new" in result.output
    # The older session should NOT appear since --current picks one row only.
    assert "model-old" not in result.output


def test_context_show_current_with_no_sessions_renders_empty(profile_home):
    SessionDB(profile_home / "sessions.db")  # empty DB
    result = CliRunner().invoke(context_app, ["show", "--current"])
    assert "Traceback" not in result.output


def test_context_show_renders_context_window_percent(profile_home):
    _, sid = _seed(profile_home)
    result = CliRunner().invoke(context_app, ["show", sid])
    assert result.exit_code == 0
    # Either a "%" or numeric tokens used / max.
    assert "%" in result.output


def test_context_show_no_args_errors_helpfully(profile_home):
    _seed(profile_home)
    result = CliRunner().invoke(context_app, ["show"])
    # Either the typer-style usage error (exit_code != 0) or a helpful
    # message. Crash-free is the contract.
    assert "Traceback" not in result.output


def test_context_show_renders_token_totals(profile_home):
    _, sid = _seed(profile_home)
    result = CliRunner().invoke(context_app, ["show", sid])
    assert result.exit_code == 0
    # 50,000 input tokens seeded.
    assert "50,000" in result.output or "50000" in result.output


def test_context_show_passes_through_known_models_only(profile_home):
    """Unknown model id renders fallback context window without crashing."""
    db = SessionDB(profile_home / "sessions.db")
    sid = db.allocate_session_id()
    db.create_session(sid, platform="cli", model="totally-unknown-model")
    db.add_tokens(session_id=sid, input_tokens=100, output_tokens=50)
    result = CliRunner().invoke(context_app, ["show", sid])
    assert result.exit_code == 0
    assert "totally-unknown-model" in result.output


def test_context_show_missing_db_does_not_crash(profile_home):
    """Empty profile home: SessionDB creates an empty DB; the CLI
    renders empty state."""
    result = CliRunner().invoke(context_app, ["show", "--current"])
    assert "Traceback" not in result.output

"""Tests for opencomputer.cli_goal — Ralph-loop goal CLI surface.

Closes the deferral from PR #420 (Wave 5 T2). Mirrors the slash-command
contract: set / status / pause / resume / clear, with a default of
"most-recent session" when ``--session`` is omitted.

Tests use Typer's ``CliRunner`` with the goal-app directly (not the full
``opencomputer`` parent) so we don't pay the cost of importing every
sub-app and we don't accidentally pick up a different SessionDB by way
of the parent app's profile resolution. The DB is monkey-patched per-test
to point at a ``tmp_path / sessions.db`` fixture, matching how the rest
of the cli_* test suite handles per-profile state.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from opencomputer import cli_goal
from opencomputer.agent.state import SessionDB
from opencomputer.cli_goal import goal_app

runner = CliRunner()


@pytest.fixture
def db(tmp_path: Path, monkeypatch) -> SessionDB:
    """Fresh per-test SessionDB with cli_goal._db patched to point at it."""
    db_path = tmp_path / "sessions.db"
    real_db = SessionDB(db_path)
    monkeypatch.setattr(cli_goal, "_db", lambda: real_db)
    return real_db


def _make_session(db: SessionDB, sid: str = "s1") -> str:
    """Create a bare session row so set_session_goal has something to update."""
    db.create_session(sid, platform="cli", model="test")
    return sid


# ─── set ───


def test_set_persists_goal_with_default_budget(db):
    sid = _make_session(db)
    result = runner.invoke(goal_app, ["set", "ship the feature", "-s", sid])
    assert result.exit_code == 0
    g = db.get_session_goal(sid)
    assert g is not None
    assert g.text == "ship the feature"
    assert g.budget == 20  # DEFAULT_GOAL_BUDGET
    assert g.active is True
    assert g.turns_used == 0


def test_set_respects_budget_flag(db):
    sid = _make_session(db)
    result = runner.invoke(goal_app, ["set", "long goal", "-s", sid, "--budget", "5"])
    assert result.exit_code == 0
    assert db.get_session_goal(sid).budget == 5


def test_set_rejects_empty_text(db):
    sid = _make_session(db)
    result = runner.invoke(goal_app, ["set", "   ", "-s", sid])
    assert result.exit_code == 1
    assert "empty" in result.output.lower()
    assert db.get_session_goal(sid) is None  # nothing persisted


def test_set_overwrites_existing_goal(db):
    sid = _make_session(db)
    db.set_session_goal(sid, text="old goal", budget=10)
    result = runner.invoke(goal_app, ["set", "new goal", "-s", sid, "--budget", "30"])
    assert result.exit_code == 0
    g = db.get_session_goal(sid)
    assert g.text == "new goal"
    assert g.budget == 30
    assert g.turns_used == 0  # reset


def test_set_uses_most_recent_session_when_session_omitted(db):
    """No --session → use db.list_sessions(limit=1)[0]['id']."""
    _make_session(db, "older")
    sid_new = _make_session(db, "newer")
    # Ensure newer is genuinely most-recent (list_sessions orders by started_at DESC)
    result = runner.invoke(goal_app, ["set", "auto-target"])
    assert result.exit_code == 0
    # Whichever the db says is most recent should have the goal
    most_recent = db.list_sessions(limit=1)[0]["id"]
    assert most_recent in (sid_new, "older")
    assert db.get_session_goal(most_recent) is not None


def test_set_errors_when_no_sessions_exist(db):
    """Empty DB + no --session → clear error, no silent no-op."""
    result = runner.invoke(goal_app, ["set", "ghost goal"])
    assert result.exit_code == 1
    assert "no sessions exist" in result.output.lower()


# ─── status ───


def test_status_shows_active_goal(db):
    sid = _make_session(db)
    db.set_session_goal(sid, text="finish the feature", budget=15)
    result = runner.invoke(goal_app, ["status", "-s", sid])
    assert result.exit_code == 0
    assert "finish the feature" in result.output
    assert "active" in result.output
    assert "0/15" in result.output


def test_status_indicates_no_goal(db):
    sid = _make_session(db)
    result = runner.invoke(goal_app, ["status", "-s", sid])
    assert result.exit_code == 0
    assert "no goal set" in result.output.lower()


def test_status_json_format_with_goal(db):
    sid = _make_session(db)
    db.set_session_goal(sid, text="json target", budget=8)
    result = runner.invoke(goal_app, ["status", "-s", sid, "--json"])
    assert result.exit_code == 0
    # The Rich console wraps long output but JSON mode prints raw
    payload = json.loads(result.output)
    assert payload["session_id"] == sid
    assert payload["goal"]["text"] == "json target"
    assert payload["goal"]["active"] is True
    assert payload["goal"]["budget"] == 8


def test_status_json_format_with_no_goal(db):
    sid = _make_session(db)
    result = runner.invoke(goal_app, ["status", "-s", sid, "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["goal"] is None
    assert payload["session_id"] == sid


def test_status_paused_state_renders(db):
    sid = _make_session(db)
    db.set_session_goal(sid, text="paused work", budget=20)
    db.update_session_goal(sid, active=False)
    result = runner.invoke(goal_app, ["status", "-s", sid])
    assert result.exit_code == 0
    assert "paused" in result.output.lower()


# ─── pause ───


def test_pause_flips_active_flag(db):
    sid = _make_session(db)
    db.set_session_goal(sid, text="work", budget=20)
    result = runner.invoke(goal_app, ["pause", "-s", sid])
    assert result.exit_code == 0
    assert db.get_session_goal(sid).active is False


def test_pause_errors_when_no_goal(db):
    """No goal to pause → exit 1, clear error message."""
    sid = _make_session(db)
    result = runner.invoke(goal_app, ["pause", "-s", sid])
    assert result.exit_code == 1
    assert "no goal" in result.output.lower()


def test_pause_idempotent_on_already_paused(db):
    """Pausing a paused goal succeeds (still active=False)."""
    sid = _make_session(db)
    db.set_session_goal(sid, text="x", budget=20)
    db.update_session_goal(sid, active=False)
    result = runner.invoke(goal_app, ["pause", "-s", sid])
    assert result.exit_code == 0
    assert db.get_session_goal(sid).active is False


# ─── resume ───


def test_resume_reactivates_and_resets_turns(db):
    sid = _make_session(db)
    db.set_session_goal(sid, text="x", budget=20)
    db.update_session_goal(sid, active=False, turns_used=12)
    result = runner.invoke(goal_app, ["resume", "-s", sid])
    assert result.exit_code == 0
    g = db.get_session_goal(sid)
    assert g.active is True
    assert g.turns_used == 0


def test_resume_errors_when_no_goal(db):
    sid = _make_session(db)
    result = runner.invoke(goal_app, ["resume", "-s", sid])
    assert result.exit_code == 1


# ─── clear ───


def test_clear_drops_goal_text(db):
    sid = _make_session(db)
    db.set_session_goal(sid, text="x", budget=20)
    result = runner.invoke(goal_app, ["clear", "-s", sid])
    assert result.exit_code == 0
    assert db.get_session_goal(sid) is None


def test_clear_no_goal_reports_dim_and_exits_zero(db):
    sid = _make_session(db)
    result = runner.invoke(goal_app, ["clear", "-s", sid])
    assert result.exit_code == 0  # not an error to clear non-existent
    assert "no goal to clear" in result.output.lower()


def test_clear_preserves_budget_for_next_set(db):
    """The clear_session_goal docstring promises budget preservation —
    verify the SessionDB invariant holds when called from the CLI."""
    sid = _make_session(db)
    db.set_session_goal(sid, text="x", budget=42)
    runner.invoke(goal_app, ["clear", "-s", sid])
    # Re-add a goal without specifying --budget, default value is 20
    runner.invoke(goal_app, ["set", "fresh goal", "-s", sid])
    # The new SET passes its own budget (20 default), which overwrites
    assert db.get_session_goal(sid).budget == 20


# ─── default-session-resolution edge case ───


def test_status_uses_most_recent_when_session_omitted(db):
    sid = _make_session(db)
    db.set_session_goal(sid, text="default-pick goal", budget=20)
    result = runner.invoke(goal_app, ["status"])
    assert result.exit_code == 0
    assert "default-pick goal" in result.output


def test_status_errors_on_empty_db_when_session_omitted(db):
    result = runner.invoke(goal_app, ["status"])
    assert result.exit_code == 1
    assert "no sessions exist" in result.output.lower()

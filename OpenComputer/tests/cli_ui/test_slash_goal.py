"""Tests for the /goal slash handler — set/status/pause/resume/clear roundtrip."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from opencomputer.agent.state import SessionDB
from opencomputer.cli_ui.slash import resolve_command
from opencomputer.cli_ui.slash_handlers import (
    SlashContext,
    _handle_goal,
)


@dataclass
class _StubSessionConfig:
    db_path: Path


@dataclass
class _StubConfig:
    session: _StubSessionConfig


def _make_ctx(
    tmp_path: Path, *, running: bool = False,
) -> tuple[SlashContext, str, SessionDB, "io.StringIO"]:
    """Build a slash context. Returns the ``io.StringIO`` buffer too so
    tests that need to inspect rendered output can read ``buf.getvalue()``.
    """
    db_path = tmp_path / "slash_goal.db"
    db = SessionDB(db_path)
    import uuid
    sid = str(uuid.uuid4())
    db.create_session(sid, platform="cli")
    cfg = _StubConfig(session=_StubSessionConfig(db_path=db_path))
    import io
    buf = io.StringIO()
    ctx = SlashContext(
        console=Console(file=buf, width=120, force_terminal=False),
        session_id=sid,
        config=cfg,
        on_clear=lambda: None,
        get_cost_summary=lambda: {},
        get_session_list=lambda: [],
        is_running_agent=lambda: running,
    )
    return ctx, sid, db, buf


def test_goal_command_registered():
    """/goal should be discoverable via the slash registry."""
    cmd = resolve_command("goal")
    assert cmd is not None
    assert cmd.name == "goal"
    assert "goal" in cmd.description.lower()


def test_goal_status_when_unset(tmp_path: Path):
    ctx, sid, db, _ = _make_ctx(tmp_path)
    r = _handle_goal(ctx, [])
    assert r.handled is True
    assert db.get_session_goal(sid) is None


def test_goal_set_then_status(tmp_path: Path):
    ctx, sid, db, _ = _make_ctx(tmp_path)
    r = _handle_goal(ctx, ["ship", "the", "wave-5", "PR"])
    assert r.handled is True
    g = db.get_session_goal(sid)
    assert g is not None
    assert g.text == "ship the wave-5 PR"
    assert g.active is True
    assert g.turns_used == 0


def test_goal_pause_resume_clear(tmp_path: Path):
    ctx, sid, db, _ = _make_ctx(tmp_path)
    _handle_goal(ctx, ["x"])
    db.update_session_goal(sid, turns_used=4)

    _handle_goal(ctx, ["pause"])
    assert db.get_session_goal(sid).active is False

    _handle_goal(ctx, ["resume"])
    g = db.get_session_goal(sid)
    assert g.active is True
    assert g.turns_used == 0  # reset on resume

    _handle_goal(ctx, ["clear"])
    assert db.get_session_goal(sid) is None


def test_pause_resume_clear_no_goal(tmp_path: Path):
    """Edge: pause/resume/clear when no goal exists — must not crash."""
    ctx, sid, db, _ = _make_ctx(tmp_path)
    for sub in ("pause", "resume", "clear"):
        r = _handle_goal(ctx, [sub])
        assert r.handled is True
    assert db.get_session_goal(sid) is None


def test_empty_set_text_treated_as_status(tmp_path: Path):
    """Edge: bare `/goal` with no text shouldn't write a NULL goal row."""
    ctx, sid, db, _ = _make_ctx(tmp_path)
    r = _handle_goal(ctx, [])
    assert r.handled is True
    assert db.get_session_goal(sid) is None


# ─── v2: rich UX strings + mid-run guard ────────────────────────────────


def test_v2_set_uses_circled_dot_icon(tmp_path: Path):
    """⊙ Goal set ({budget}-turn budget): <preview>."""
    ctx, _sid, _db, buf = _make_ctx(tmp_path)
    _handle_goal(ctx, ["create", "4", "files"])
    out = buf.getvalue()
    assert "⊙" in out
    assert "Goal set" in out
    assert "20-turn budget" in out  # default budget surfaces in UX


def test_v2_status_shows_last_judge_reason(tmp_path: Path):
    ctx, sid, db, buf = _make_ctx(tmp_path)
    db.set_session_goal(sid, text="ship", budget=20)
    db.update_session_goal(sid, last_judge_reason="halfway done")
    _handle_goal(ctx, ["status"])
    out = buf.getvalue()
    assert "halfway done" in out
    assert "last judge" in out


def test_v2_status_omits_reason_when_unset(tmp_path: Path):
    ctx, sid, db, buf = _make_ctx(tmp_path)
    db.set_session_goal(sid, text="x", budget=20)
    _handle_goal(ctx, ["status"])
    out = buf.getvalue()
    assert "last judge" not in out


def test_v2_pause_resume_clear_use_icons(tmp_path: Path):
    ctx, sid, db, buf = _make_ctx(tmp_path)
    _handle_goal(ctx, ["x"])
    buf.truncate(0); buf.seek(0)
    _handle_goal(ctx, ["pause"])
    assert "⏸" in buf.getvalue()
    buf.truncate(0); buf.seek(0)
    _handle_goal(ctx, ["resume"])
    assert "↻" in buf.getvalue()
    buf.truncate(0); buf.seek(0)
    _handle_goal(ctx, ["clear"])
    assert "✗" in buf.getvalue()


def test_v2_resume_clears_last_judge_reason(tmp_path: Path):
    """Resume is a fresh start — old judge reason gets nulled."""
    ctx, sid, db, _buf = _make_ctx(tmp_path)
    _handle_goal(ctx, ["ship"])
    db.update_session_goal(sid, last_judge_reason="midway")
    _handle_goal(ctx, ["resume"])
    g = db.get_session_goal(sid)
    assert g is not None
    assert g.last_judge_reason is None


def test_v2_status_budget_exhausted_shows_pause_banner(tmp_path: Path):
    ctx, sid, db, buf = _make_ctx(tmp_path)
    db.set_session_goal(sid, text="x", budget=3)
    db.update_session_goal(sid, turns_used=3)
    _handle_goal(ctx, ["status"])
    out = buf.getvalue()
    assert "⏸" in out
    assert "3/3" in out


def test_v2_set_form_refused_when_agent_running(tmp_path: Path):
    ctx, sid, db, buf = _make_ctx(tmp_path, running=True)
    _handle_goal(ctx, ["change", "the", "goal"])
    out = buf.getvalue()
    assert "/stop" in out
    assert db.get_session_goal(sid) is None


def test_v2_status_allowed_while_agent_running(tmp_path: Path):
    ctx, sid, db, buf = _make_ctx(tmp_path, running=True)
    db.set_session_goal(sid, text="ship", budget=20)
    _handle_goal(ctx, ["status"])
    out = buf.getvalue()
    assert "ship" in out
    assert "/stop" not in out


def test_v2_pause_resume_clear_allowed_while_running(tmp_path: Path):
    ctx, sid, db, _buf = _make_ctx(tmp_path, running=True)
    db.set_session_goal(sid, text="ship", budget=20)
    for sub in ("pause", "resume", "clear"):
        r = _handle_goal(ctx, [sub])
        assert r.handled is True
    # All three executed — final state is cleared.
    assert db.get_session_goal(sid) is None

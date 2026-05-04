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


def _make_ctx(tmp_path: Path) -> tuple[SlashContext, str, SessionDB]:
    db_path = tmp_path / "slash_goal.db"
    db = SessionDB(db_path)
    import uuid
    sid = str(uuid.uuid4())
    db.create_session(sid, platform="cli")
    cfg = _StubConfig(session=_StubSessionConfig(db_path=db_path))
    import io
    ctx = SlashContext(
        console=Console(file=io.StringIO()),  # discard output in tests
        session_id=sid,
        config=cfg,
        on_clear=lambda: None,
        get_cost_summary=lambda: {},
        get_session_list=lambda: [],
    )
    return ctx, sid, db


def test_goal_command_registered():
    """/goal should be discoverable via the slash registry."""
    cmd = resolve_command("goal")
    assert cmd is not None
    assert cmd.name == "goal"
    assert "goal" in cmd.description.lower()


def test_goal_status_when_unset(tmp_path: Path):
    ctx, sid, db = _make_ctx(tmp_path)
    r = _handle_goal(ctx, [])
    assert r.handled is True
    assert db.get_session_goal(sid) is None


def test_goal_set_then_status(tmp_path: Path):
    ctx, sid, db = _make_ctx(tmp_path)
    r = _handle_goal(ctx, ["ship", "the", "wave-5", "PR"])
    assert r.handled is True
    g = db.get_session_goal(sid)
    assert g is not None
    assert g.text == "ship the wave-5 PR"
    assert g.active is True
    assert g.turns_used == 0


def test_goal_pause_resume_clear(tmp_path: Path):
    ctx, sid, db = _make_ctx(tmp_path)
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
    ctx, sid, db = _make_ctx(tmp_path)
    for sub in ("pause", "resume", "clear"):
        r = _handle_goal(ctx, [sub])
        assert r.handled is True
    assert db.get_session_goal(sid) is None


def test_empty_set_text_treated_as_status(tmp_path: Path):
    """Edge: bare `/goal` with no text shouldn't write a NULL goal row."""
    ctx, sid, db = _make_ctx(tmp_path)
    r = _handle_goal(ctx, [])
    assert r.handled is True
    assert db.get_session_goal(sid) is None

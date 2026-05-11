"""Phase I — cross-project clipboard handoff in ``oc resume``.

Coverage:

    1. ``_is_session_in_other_project`` correctly identifies same-cwd,
       repo-relative, and unrelated cwds.
    2. NULL session rows + missing cwds + DB errors all degrade to
       "same project" (resume in place, no false handoff).
    3. Path prefix check is sep-aware (``/work/p`` does NOT match
       ``/work/p2``).
    4. ``_emit_cross_project_handoff`` copies the command to the
       clipboard AND falls back gracefully when no clipboard backend
       is available (prints the command for manual copy).
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

from opencomputer.agent.state import SessionDB
from opencomputer.cli import (
    _emit_cross_project_handoff,
    _is_session_in_other_project,
)

# ─── _is_session_in_other_project ────────────────────────────────────


def _seed(db: SessionDB, sid: str, cwd: str | None) -> None:
    db.ensure_session(sid, cwd=cwd)


def test_session_with_no_recorded_cwd_is_same_project(tmp_path: Path) -> None:
    db = SessionDB(tmp_path / "sessions.db")
    sid = uuid.uuid4().hex
    _seed(db, sid, cwd=None)

    assert (
        _is_session_in_other_project(
            db=db, session_id=sid, cwd_str="/anything", repo_paths=[]
        )
        is False
    )


def test_session_under_current_cwd_is_same_project(tmp_path: Path) -> None:
    db = SessionDB(tmp_path / "sessions.db")
    sid = uuid.uuid4().hex
    _seed(db, sid, cwd="/work/proj/sub")

    assert (
        _is_session_in_other_project(
            db=db, session_id=sid, cwd_str="/work/proj", repo_paths=[]
        )
        is False
    )


def test_session_at_exact_cwd_is_same_project(tmp_path: Path) -> None:
    db = SessionDB(tmp_path / "sessions.db")
    sid = uuid.uuid4().hex
    _seed(db, sid, cwd="/work/proj")

    assert (
        _is_session_in_other_project(
            db=db, session_id=sid, cwd_str="/work/proj", repo_paths=[]
        )
        is False
    )


def test_session_under_a_repo_worktree_is_same_project(tmp_path: Path) -> None:
    """Session lives under one of the supplied repo worktree roots."""
    db = SessionDB(tmp_path / "sessions.db")
    sid = uuid.uuid4().hex
    _seed(db, sid, cwd="/work/proj-worktree/feature/sub")

    assert (
        _is_session_in_other_project(
            db=db,
            session_id=sid,
            cwd_str="/work/proj",
            repo_paths=["/work/proj-worktree"],
        )
        is False
    )


def test_session_in_completely_unrelated_dir_is_other_project(tmp_path: Path) -> None:
    db = SessionDB(tmp_path / "sessions.db")
    sid = uuid.uuid4().hex
    _seed(db, sid, cwd="/elsewhere/somethingelse")

    assert (
        _is_session_in_other_project(
            db=db, session_id=sid, cwd_str="/work/proj", repo_paths=[]
        )
        is True
    )


def test_prefix_check_is_separator_aware(tmp_path: Path) -> None:
    """``/work/proj`` must NOT match a session under ``/work/proj-other``."""
    db = SessionDB(tmp_path / "sessions.db")
    sid = uuid.uuid4().hex
    _seed(db, sid, cwd="/work/proj-other/file")

    assert (
        _is_session_in_other_project(
            db=db, session_id=sid, cwd_str="/work/proj", repo_paths=[]
        )
        is True
    )


def test_missing_session_id_returns_false(tmp_path: Path) -> None:
    db = SessionDB(tmp_path / "sessions.db")
    # session is never created — get_session returns None.
    assert (
        _is_session_in_other_project(
            db=db, session_id="ghost", cwd_str="/work", repo_paths=[]
        )
        is False
    )


def test_empty_session_id_returns_false(tmp_path: Path) -> None:
    db = SessionDB(tmp_path / "sessions.db")
    assert (
        _is_session_in_other_project(
            db=db, session_id="", cwd_str="/work", repo_paths=[]
        )
        is False
    )


def test_db_error_during_lookup_falls_back_to_same_project() -> None:
    """A broken DB must NOT crash the picker — degrade to 'same'."""

    class _BrokenDB:
        def get_session(self, _sid: str):  # noqa: ANN201
            raise RuntimeError("disk full")

    assert (
        _is_session_in_other_project(
            db=_BrokenDB(), session_id="x", cwd_str="/work", repo_paths=[]
        )
        is False
    )


def test_normpath_handles_trailing_slash(tmp_path: Path) -> None:
    """``/work/proj/`` (trailing slash) must match a session at ``/work/proj``."""
    db = SessionDB(tmp_path / "sessions.db")
    sid = uuid.uuid4().hex
    _seed(db, sid, cwd="/work/proj")

    assert (
        _is_session_in_other_project(
            db=db, session_id=sid, cwd_str="/work/proj/", repo_paths=[]
        )
        is False
    )


def test_empty_strings_in_repo_paths_are_skipped(tmp_path: Path) -> None:
    """A spurious empty string in repo_paths must not match every cwd."""
    db = SessionDB(tmp_path / "sessions.db")
    sid = uuid.uuid4().hex
    _seed(db, sid, cwd="/elsewhere")

    assert (
        _is_session_in_other_project(
            db=db,
            session_id=sid,
            cwd_str="/work/proj",
            repo_paths=["", "/work/proj-worktree"],
        )
        is True
    )


# ─── _emit_cross_project_handoff ──────────────────────────────────────


def test_handoff_copies_command_to_clipboard(tmp_path: Path) -> None:
    """Happy path: pyperclip succeeds, hint is printed."""
    db = SessionDB(tmp_path / "sessions.db")
    sid = uuid.uuid4().hex
    db.ensure_session(sid, cwd="/work/other/proj")

    with patch("pyperclip.copy") as mock_copy:
        result = _emit_cross_project_handoff(db=db, session_id=sid)

    assert result is True
    mock_copy.assert_called_once()
    cmd = mock_copy.call_args[0][0]
    assert "cd /work/other/proj" in cmd
    assert f"oc resume {sid}" in cmd


def test_handoff_quotes_paths_with_spaces(tmp_path: Path) -> None:
    """A cwd with spaces must round-trip through shell quoting."""
    db = SessionDB(tmp_path / "sessions.db")
    sid = uuid.uuid4().hex
    db.ensure_session(sid, cwd="/work/my project")

    with patch("pyperclip.copy") as mock_copy:
        _emit_cross_project_handoff(db=db, session_id=sid)

    cmd = mock_copy.call_args[0][0]
    # shlex.quote wraps with single quotes:
    assert "'/work/my project'" in cmd


def test_handoff_returns_false_when_session_missing(tmp_path: Path) -> None:
    db = SessionDB(tmp_path / "sessions.db")
    result = _emit_cross_project_handoff(db=db, session_id="ghost-id")
    assert result is False


def test_handoff_returns_false_when_session_has_no_cwd(tmp_path: Path) -> None:
    db = SessionDB(tmp_path / "sessions.db")
    sid = uuid.uuid4().hex
    db.ensure_session(sid, cwd=None)
    result = _emit_cross_project_handoff(db=db, session_id=sid)
    assert result is False


def test_handoff_falls_back_when_pyperclip_raises(tmp_path: Path) -> None:
    """No clipboard backend → still surface the command (return True)."""
    db = SessionDB(tmp_path / "sessions.db")
    sid = uuid.uuid4().hex
    db.ensure_session(sid, cwd="/work/other")

    with patch("pyperclip.copy", side_effect=RuntimeError("no clipboard backend")):
        result = _emit_cross_project_handoff(db=db, session_id=sid)

    # Returns True because we DID surface the handoff to the user
    # (just via stdout instead of clipboard).
    assert result is True


def test_handoff_survives_db_error(tmp_path: Path) -> None:
    """DB errors during get_session don't crash the handoff."""

    class _BrokenDB:
        def get_session(self, _sid: str):  # noqa: ANN201
            raise RuntimeError("disk full")

    result = _emit_cross_project_handoff(db=_BrokenDB(), session_id="x")
    assert result is False

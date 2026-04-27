"""Tests for the input loop module — PromptSession + key bindings.

Most prompt_toolkit behavior is interactive and hard to unit-test;
these tests cover the pieces we own: history file path computation,
session builder returns a PromptSession with a FileHistory bound to the
right path, and the pure helpers (``_strip_trailing_whitespace``).
"""
from __future__ import annotations

from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory

from opencomputer.cli_ui.input_loop import (
    _history_file_path,
    _strip_trailing_whitespace,
    build_prompt_session,
)
from opencomputer.cli_ui.turn_cancel import TurnCancelScope


def test_history_file_path_under_profile_home(tmp_path: Path):
    profile = tmp_path / "myprofile"
    profile.mkdir()
    p = _history_file_path(profile)
    assert p.parent == profile
    assert p.name == "input_history"


def test_history_file_path_creates_parent_when_missing(tmp_path: Path):
    profile = tmp_path / "newprofile"  # does not exist
    p = _history_file_path(profile)
    # Path is computed; parent must exist after the call so FileHistory
    # construction doesn't fail.
    assert p.parent.exists()


def test_strip_trailing_whitespace_simple():
    assert _strip_trailing_whitespace("hello  ") == "hello"
    assert _strip_trailing_whitespace("  ") == ""
    assert _strip_trailing_whitespace("hello\nworld") == "hello\nworld"


def test_build_prompt_session_returns_session(tmp_path: Path):
    scope = TurnCancelScope()
    session = build_prompt_session(profile_home=tmp_path, scope=scope)
    assert isinstance(session, PromptSession)
    # History is a FileHistory pointing under our profile dir.
    assert isinstance(session.history, FileHistory)
    assert Path(session.history.filename).parent == tmp_path

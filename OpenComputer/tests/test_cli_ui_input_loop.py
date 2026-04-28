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


def test_build_prompt_session_has_slash_completer(tmp_path: Path):
    """PromptSession must have SlashCommandCompleter wired so the
    dropdown menu appears when the user types '/'."""
    from opencomputer.cli_ui.slash_completer import SlashCommandCompleter

    scope = TurnCancelScope()
    session = build_prompt_session(profile_home=tmp_path, scope=scope)
    assert isinstance(session.completer, SlashCommandCompleter)


def test_build_prompt_session_complete_while_typing_enabled(tmp_path: Path):
    """``complete_while_typing`` must be True so the dropdown auto-shows
    as the user types — without it, completions only fire on Tab."""
    scope = TurnCancelScope()
    session = build_prompt_session(profile_home=tmp_path, scope=scope)
    cwt = session.complete_while_typing
    if callable(cwt):
        cwt = cwt()
    assert cwt is True


def test_build_prompt_session_tab_keybinding_registered(tmp_path: Path):
    """Tab/ControlI must be bound so our LCP handler runs instead of
    falling through to prompt_toolkit's default Tab behavior."""
    from prompt_toolkit.keys import Keys

    scope = TurnCancelScope()
    session = build_prompt_session(profile_home=tmp_path, scope=scope)
    tab_keys = (Keys.Tab, Keys.ControlI)
    bindings = session.key_bindings.bindings
    assert any(
        any(k in tab_keys for k in b.keys) for b in bindings
    ), "Tab keybinding missing from PromptSession"


def test_build_prompt_session_uses_multicolumn_complete_style(tmp_path: Path):
    """The dropdown must use MULTI_COLUMN style so it renders in editor
    terminals (e.g. VS Code) that don't reliably respond to
    Cursor-Position-Report (CPR) requests. The default COLUMN style
    uses a Float widget that needs CPR; MULTI_COLUMN uses a Window
    in the main layout and works without CPR."""
    from prompt_toolkit.shortcuts import CompleteStyle

    scope = TurnCancelScope()
    session = build_prompt_session(profile_home=tmp_path, scope=scope)
    assert session.complete_style == CompleteStyle.MULTI_COLUMN


def test_read_user_input_app_layout_renders_without_crash(tmp_path: Path):
    """Regression: PR #210 introduced ``Dimension(exact=…)`` which is
    invalid (the kwarg doesn't exist on the constructor — it's a
    classmethod ``Dimension.exact(N)``). The bug only fired the moment
    the renderer asked the dropdown Window for its preferred height,
    i.e. AS SOON AS the user typed ``/``. This test exercises that
    exact path: build the chat input Application, simulate ``/`` being
    typed, then ask the layout for its preferred height. If the
    Dimension API is wrong, this raises ``TypeError`` during
    ``preferred_height`` — same crash the user hit.
    """
    import asyncio

    from prompt_toolkit.application.current import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from opencomputer.cli_ui.input_loop import read_user_input

    async def _drive():
        with create_pipe_input() as inp:
            with create_app_session(input=inp, output=DummyOutput()):
                # Kick off read_user_input as a task; we'll never let it
                # finish — we just want the Application built so we can
                # interrogate its layout.
                task = asyncio.create_task(
                    read_user_input(profile_home=tmp_path, scope=TurnCancelScope())
                )
                # Yield once so the Application is constructed.
                await asyncio.sleep(0.05)

                from prompt_toolkit.application.current import get_app

                app = get_app()
                # Simulate a slash typed into the input buffer. This is
                # what populates ``state["matches"]`` and makes the
                # dropdown ConditionalContainer go from 0-height to
                # rendering — the path that crashed.
                buf = app.current_buffer
                buf.text = "/"
                buf.cursor_position = 1

                # Ask the layout for a preferred height — this calls
                # ``_dropdown_height`` and surfaces the Dimension API bug.
                size = app.output.get_size()
                # Should not raise.
                app.layout.container.preferred_height(size.columns, size.rows)

                # Clean up.
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, EOFError, KeyboardInterrupt):
                    pass

    asyncio.run(_drive())

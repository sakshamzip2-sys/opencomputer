"""Regression tests for AskUserQuestion + KeyboardListener stdin race.

Bug (2026-05-03): When AskUserQuestion fires mid-turn, the
KeyboardListener daemon thread holds stdin in termios cbreak mode and
reads char-by-char via ``select.select`` to detect ESC. The handler
then calls ``console.input("> ")`` which uses ``builtins.input()`` —
but stdin is in cbreak mode (no line discipline) AND the listener
steals each byte before ``input()`` sees it. Result: handler blocks
forever, tool stays at "0.0s running".

Fix: the listener exposes a ``current_listener()`` global + a
``pause_for_input()`` context manager that ``stop()``s the listener
(restoring cooked-mode termios) for the duration of a blocking line
read, then re-arms it.

These tests pin the contract:
1. ``current_listener()`` tracks the active listener via start/stop.
2. ``pause_for_input()`` stops the listener while inside the block
   and re-arms it on exit.
3. The handler discovers and uses the current listener if present.
4. The handler is no-op-safe when no listener is registered (CLI
   plain-stream / piped-input modes).
"""
from __future__ import annotations

import asyncio
import io
from unittest.mock import MagicMock, patch

from rich.console import Console

from opencomputer.cli_ui import keyboard_listener as kl
from opencomputer.cli_ui.ask_user_question_handler import make_rich_handler
from plugin_sdk.interaction import InteractionRequest


def _make_console_with_input(text: str) -> Console:
    out = io.StringIO()
    console = Console(file=out, force_terminal=False, width=80)
    console.input = lambda prompt="": text  # type: ignore[method-assign]
    return console


def test_current_listener_starts_unset() -> None:
    """No listener => current_listener() returns None.

    Important so the handler's ``if listener is not None`` guard works
    in non-TTY / piped-input / test contexts where no listener was
    ever started.
    """
    # Reset just in case a previous test leaked state.
    kl._CURRENT_LISTENER = None
    assert kl.current_listener() is None


def test_listener_registers_self_in_current_on_start() -> None:
    """start() must publish the listener to the global registry so
    the handler can find it via current_listener()."""
    kl._CURRENT_LISTENER = None
    listener = kl.KeyboardListener(scope=MagicMock())
    # Force the headless/no-tty short-circuit so we don't actually
    # try to grab termios in the test environment, but DO exercise
    # the registry side-effect path.
    with patch.object(kl, "sys") as fake_sys:
        fake_sys.stdin.isatty = lambda: False  # short-circuit before threading
        listener.start()
    # Even though the thread isn't running (no TTY), the listener
    # is registered as "active" so the handler can pause it.
    assert kl.current_listener() is listener


def test_listener_clears_self_from_current_on_stop() -> None:
    """stop() must un-register so a stale listener can't be paused
    after its turn ended."""
    kl._CURRENT_LISTENER = None
    listener = kl.KeyboardListener(scope=MagicMock())
    with patch.object(kl, "sys") as fake_sys:
        fake_sys.stdin.isatty = lambda: False
        listener.start()
    assert kl.current_listener() is listener
    listener.stop()
    assert kl.current_listener() is None


def test_pause_for_input_stops_and_resumes_listener() -> None:
    """pause_for_input() context manager must call stop() on entry
    and start() on exit, so the blocking line read sees cooked-mode
    stdin without the daemon thread stealing keystrokes."""
    kl._CURRENT_LISTENER = None
    listener = kl.KeyboardListener(scope=MagicMock())
    calls: list[str] = []

    with (
        patch.object(listener, "start", side_effect=lambda: calls.append("start")),
        patch.object(listener, "stop", side_effect=lambda: calls.append("stop")),
    ):
        # Pretend the listener is running so pause needs to stop it.
        listener._thread = MagicMock(is_alive=lambda: True)
        with listener.pause_for_input():
            calls.append("inside")
        # stop must run before the body; start must run after.
        assert calls == ["stop", "inside", "start"]


def test_pause_for_input_no_op_when_listener_inactive() -> None:
    """If the listener was never started (or already stopped), pause
    must NOT call start() on exit — that would spuriously arm a
    listener the caller never wanted running."""
    listener = kl.KeyboardListener(scope=MagicMock())
    listener._thread = None  # not running
    calls: list[str] = []
    with (
        patch.object(listener, "start", side_effect=lambda: calls.append("start")),
        patch.object(listener, "stop", side_effect=lambda: calls.append("stop")),
    ):
        with listener.pause_for_input():
            calls.append("inside")
    # Neither start nor stop should fire — the listener was already idle.
    assert calls == ["inside"]


def test_handler_pauses_listener_around_console_input() -> None:
    """End-to-end: when a current_listener exists, the handler must
    wrap its console.input call in pause_for_input(). Without this
    the daemon thread + input() race makes the tool hang.

    Spy on the listener's pause_for_input to confirm the handler
    enters and exits the context around the console.input call.
    """
    kl._CURRENT_LISTENER = None
    listener = kl.KeyboardListener(scope=MagicMock())
    listener._thread = MagicMock(is_alive=lambda: True)
    kl._CURRENT_LISTENER = listener

    pause_enter_called = False
    pause_exit_called = False
    input_called_inside_pause = False

    from contextlib import contextmanager

    @contextmanager
    def fake_pause():
        nonlocal pause_enter_called, pause_exit_called
        pause_enter_called = True
        try:
            yield
        finally:
            pause_exit_called = True

    console = _make_console_with_input("hello")
    real_input = console.input

    def spy_input(prompt=""):
        nonlocal input_called_inside_pause
        input_called_inside_pause = pause_enter_called and not pause_exit_called
        return real_input(prompt)

    console.input = spy_input  # type: ignore[method-assign]

    with patch.object(listener, "pause_for_input", side_effect=fake_pause):
        handler = make_rich_handler(console=console)
        resp = asyncio.run(handler(InteractionRequest(question="?")))

    kl._CURRENT_LISTENER = None  # cleanup

    assert pause_enter_called, "handler must enter pause_for_input"
    assert pause_exit_called, "handler must exit pause_for_input"
    assert input_called_inside_pause, "console.input must run INSIDE the pause window"
    assert resp.text == "hello"


def test_handler_works_when_no_listener_registered() -> None:
    """Plain-stream / non-TTY code paths register no listener. The
    handler must still work — just call console.input directly."""
    kl._CURRENT_LISTENER = None
    console = _make_console_with_input("answer")
    handler = make_rich_handler(console=console)
    resp = asyncio.run(handler(InteractionRequest(question="?")))
    assert resp.text == "answer"

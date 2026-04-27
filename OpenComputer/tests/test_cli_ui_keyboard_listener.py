"""Tests for KeyboardListener — daemon thread reading stdin for ESC."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from opencomputer.cli_ui.keyboard_listener import (
    ESC_BYTE,
    KeyboardListener,
    _is_esc,
)
from opencomputer.cli_ui.turn_cancel import TurnCancelScope


def test_esc_byte_constant():
    assert ESC_BYTE == b"\x1b"


def test_is_esc_recognizes_esc():
    assert _is_esc(b"\x1b") is True
    assert _is_esc(b"\x1b\x1b") is True  # Esc Esc still starts with Esc
    assert _is_esc(b"a") is False
    assert _is_esc(b"") is False


@pytest.mark.asyncio
async def test_listener_no_op_on_non_tty():
    """If stdin isn't a TTY (CI / pipe), start() must return immediately
    without touching termios — otherwise tests crash."""
    scope = TurnCancelScope()
    listener = KeyboardListener(scope)
    with patch("sys.stdin") as mock_stdin:
        mock_stdin.isatty.return_value = False
        listener.start()  # must NOT raise
        listener.stop()


def test_listener_start_stop_idempotent():
    """Calling start/stop twice in a row must not raise."""
    scope = TurnCancelScope()
    listener = KeyboardListener(scope)
    listener.start()
    listener.start()  # second start is a no-op (or no-op on non-TTY)
    listener.stop()
    listener.stop()  # second stop is a no-op


@pytest.mark.asyncio
async def test_listener_processes_esc_byte_via_handle():
    """The internal byte handler sets the scope cancel flag on ESC."""
    scope = TurnCancelScope()
    listener = KeyboardListener(scope)
    listener._handle_byte(b"\x1b")
    assert scope.is_cancelled() is True


@pytest.mark.asyncio
async def test_listener_ignores_non_esc_bytes():
    scope = TurnCancelScope()
    listener = KeyboardListener(scope)
    listener._handle_byte(b"a")
    listener._handle_byte(b"\n")
    listener._handle_byte(b"")
    assert scope.is_cancelled() is False

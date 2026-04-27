"""Daemon-thread stdin reader that fires cancel on ESC during a turn.

prompt_toolkit's ESC binding handles the *prompt* phase (clearing the
input buffer), but during the streaming phase prompt_toolkit isn't
the active app — keystrokes go to the terminal and nothing reads
them. To honor the user's "ESC interrupts streaming" expectation we
need a separate raw-mode stdin reader that runs only while the turn
is in flight.

Pattern adapted from kimi-cli's ``KeyboardListener`` (which uses a
daemon thread + termios cbreak mode). The thread is started when a
turn begins and stopped when the turn ends; only one listener is
active at a time. On non-TTY stdin (CI, piped input) the listener is
a no-op so tests and ``printf … | opencomputer chat`` pipelines
don't crash.

The listener does NOT consume non-ESC bytes — they sit in the OS
buffer. That's by design: prompt_toolkit's next ``prompt_async()``
call drains them when the prompt re-activates. So typing characters
during streaming pre-loads the next turn's prompt; only ESC short-
circuits.
"""
from __future__ import annotations

import logging
import os
import select
import sys
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opencomputer.cli_ui.turn_cancel import TurnCancelScope

_log = logging.getLogger("opencomputer.cli_ui.keyboard_listener")

#: The single byte that means "user pressed ESC". Sequences like arrow
#: keys also start with this byte (``\x1b[A``), but we treat any byte
#: starting with ``\x1b`` as a cancel intent — the user can't realistically
#: press an arrow key during streaming since the prompt isn't focused.
ESC_BYTE: bytes = b"\x1b"


def _is_esc(b: bytes) -> bool:
    return b.startswith(ESC_BYTE) if b else False


class KeyboardListener:
    """Background thread that reads stdin and fires ESC cancellation.

    Lifecycle::

        listener = KeyboardListener(scope)
        listener.start()
        try:
            ...  # run the streaming turn
        finally:
            listener.stop()
    """

    def __init__(self, scope: TurnCancelScope) -> None:
        self._scope = scope
        self._thread: threading.Thread | None = None
        self._stop_event: threading.Event = threading.Event()

    def start(self) -> None:
        """Begin watching stdin. No-op if stdin isn't a TTY or already running."""
        if self._thread is not None and self._thread.is_alive():
            return
        if not sys.stdin.isatty():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="oc-keyboard-listener", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop the listener thread. Idempotent."""
        self._stop_event.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=0.5)
        self._thread = None

    def _handle_byte(self, b: bytes) -> None:
        """Pure helper — exposed for tests. Cancels scope on ESC."""
        if _is_esc(b):
            self._scope.request_cancel()

    def _run(self) -> None:
        """Thread body. Sets stdin into cbreak mode so reads are
        unbuffered, then loops on ``select`` + ``read(1)``."""
        try:
            import termios
            import tty
        except ImportError:
            # Windows: termios is unavailable. Fall through to no-op.
            return

        fd = sys.stdin.fileno()
        try:
            old_attrs = termios.tcgetattr(fd)
        except (termios.error, OSError):
            return  # not a real TTY despite isatty (e.g. SSH edge case)
        try:
            tty.setcbreak(fd)
            while not self._stop_event.is_set():
                # 0.1s poll keeps stop() responsive.
                r, _, _ = select.select([fd], [], [], 0.1)
                if not r:
                    continue
                try:
                    b = os.read(fd, 1)
                except OSError:
                    break
                if not b:
                    break
                self._handle_byte(b)
                if self._scope.is_cancelled():
                    # Don't process further bytes after cancel — leave them
                    # in the OS buffer for the next prompt to drain.
                    break
        except Exception as exc:  # noqa: BLE001 — must never crash the chat loop
            _log.debug("keyboard listener errored: %s", exc)
        finally:
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)
            except (termios.error, OSError):
                pass

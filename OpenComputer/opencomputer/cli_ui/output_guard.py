"""Stdout takeover guard for the interactive TUI.

Ported from pi's ``packages/coding-agent/src/core/output-guard.ts``
(2026-05-11).

Why this exists: prompt_toolkit owns the rendered screen. Any stray
``print()`` from a background tool, a library, or a misbehaving plugin
bypasses Rich and corrupts the layout — the cursor jumps, the status
line gets clobbered, the input prompt loses its place. Symptoms include
"why is half my status_line missing?" and "my input got eaten."

Pi solves this by monkey-patching ``process.stdout.write`` to redirect
to stderr while the TUI is active. We do the same for Python's
``sys.stdout``: replace with a writer that routes to ``sys.stderr``
unless the caller used the escape hatch :func:`write_raw_stdout`.

Public surface:

* :func:`take_over_stdout` — start guarding (idempotent).
* :func:`restore_stdout` — stop guarding (safe to call without takeover).
* :func:`write_raw_stdout` — intentional stdout write, bypasses the guard.
* :func:`is_stdout_taken_over` — state query.
* :class:`OutputGuardError` — raised when ``strict=True`` and a hostile
  stdout swap was detected.
"""

from __future__ import annotations

import sys
from io import TextIOBase
from typing import IO, Any, Final


class OutputGuardError(RuntimeError):
    """Raised by :func:`take_over_stdout` with ``strict=True`` when
    ``sys.stdout`` has already been replaced by an object whose
    ``.write`` we don't trust."""


class _GuardedStdout(TextIOBase):
    """Writer that intercepts every write and redirects it to stderr.

    Exists as a small ``TextIOBase`` subclass so anything that does
    ``isinstance(sys.stdout, io.TextIOBase)`` keeps working. The
    redirect lives in :meth:`write`; the rest of the API forwards to
    the underlying stderr stream so flush / fileno / isatty behave
    correctly."""

    def __init__(self, target: IO[str]) -> None:
        self._target = target

    def write(self, s: str) -> int:  # type: ignore[override]
        return self._target.write(s)

    def flush(self) -> None:  # type: ignore[override]
        self._target.flush()

    def fileno(self) -> int:  # type: ignore[override]
        return self._target.fileno()

    def isatty(self) -> bool:  # type: ignore[override]
        try:
            return self._target.isatty()
        except (AttributeError, ValueError):
            return False

    def writable(self) -> bool:  # type: ignore[override]
        return True


#: Internal state captured at takeover so :func:`restore_stdout` can
#: undo the swap without trampling anything that ran in between.
_state: dict[str, Any] | None = None

#: Module-level so tests can monkey-patch a known-good stderr if
#: pytest's capture changes them mid-run.
_STDERR_NAME: Final[str] = "stderr"


def is_stdout_taken_over() -> bool:
    """Return ``True`` iff the guard is currently active."""
    return _state is not None


def take_over_stdout(*, strict: bool = False) -> None:
    """Start redirecting all writes from ``sys.stdout`` to ``sys.stderr``.

    Idempotent — a second call while already active is a no-op so the
    TUI startup path can call this defensively without worrying about
    being re-entered.

    Args:
        strict: When ``True``, refuse to take over if ``sys.stdout``
            has already been replaced by something whose ``.write`` we
            don't own (e.g. pytest's ``CaptureFixture``). Pass ``True``
            in production startup; leave ``False`` (the default) for
            tests that use ``capsys``.
    """
    global _state
    if _state is not None:
        return

    original_stdout = sys.stdout
    if strict and original_stdout is not sys.__stdout__:
        raise OutputGuardError(
            f"refusing takeover: sys.stdout is {type(original_stdout).__name__}, "
            "not the original sys.__stdout__ — another tool has already replaced it"
        )

    guarded = _GuardedStdout(getattr(sys, _STDERR_NAME))
    _state = {"original_stdout": original_stdout, "guarded": guarded}
    sys.stdout = guarded  # type: ignore[assignment]


def restore_stdout() -> None:
    """Restore the original ``sys.stdout``. Safe to call when no
    takeover is active — returns without doing anything in that case."""
    global _state
    if _state is None:
        return
    sys.stdout = _state["original_stdout"]
    _state = None


def write_raw_stdout(text: str) -> None:
    """Bypass the guard and write directly to the real stdout.

    Use this for output that is *meant* to appear on stdout even while
    the TUI is rendering — exit codes, JSON for ``oc chat -q``, that
    sort of thing. When no takeover is active, falls back to a plain
    ``sys.stdout.write``."""
    if _state is not None:
        original = _state["original_stdout"]
        original.write(text)
        try:
            original.flush()
        except (OSError, ValueError):
            pass
        return
    sys.stdout.write(text)
    try:
        sys.stdout.flush()
    except (OSError, ValueError):
        pass

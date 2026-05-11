"""Tests for ``opencomputer/cli_ui/output_guard.py``.

Ported from pi's ``packages/coding-agent/src/core/output-guard.ts``.

Why this exists: when the TUI owns the screen (prompt_toolkit holds a
render buffer + cursor), any stray ``print()`` from a background tool
or library bypasses Rich and corrupts the layout — the cursor jumps,
the status_line gets clobbered, the input prompt loses its place.

pi solves this by monkey-patching ``process.stdout.write`` to redirect
to stderr while the TUI is active. We do the same for Python's
``sys.stdout``: replace with a writer that routes to ``sys.stderr``
unless the caller used the escape hatch :func:`write_raw_stdout`.

These tests verify:
1. ``take_over_stdout`` is idempotent (calling twice is a no-op).
2. After takeover, ``print()`` goes to stderr, not stdout.
3. ``write_raw_stdout`` bypasses the guard.
4. ``restore_stdout`` undoes everything.
5. The guard exposes its state via ``is_stdout_taken_over``.
"""

from __future__ import annotations

import io
import sys

import pytest

from opencomputer.cli_ui.output_guard import (
    OutputGuardError,
    is_stdout_taken_over,
    restore_stdout,
    take_over_stdout,
    write_raw_stdout,
)


@pytest.fixture(autouse=True)
def _restore_after_each() -> None:
    """Ensure tests can't leak guard state to siblings."""
    yield
    if is_stdout_taken_over():
        restore_stdout()


class TestTakeoverLifecycle:
    def test_not_active_by_default(self) -> None:
        assert is_stdout_taken_over() is False

    def test_takeover_then_restore(self) -> None:
        take_over_stdout()
        assert is_stdout_taken_over() is True
        restore_stdout()
        assert is_stdout_taken_over() is False

    def test_double_takeover_is_idempotent(self) -> None:
        take_over_stdout()
        take_over_stdout()  # No-op.
        assert is_stdout_taken_over() is True
        restore_stdout()  # Only one restore needed even after two takeovers.
        assert is_stdout_taken_over() is False

    def test_restore_without_takeover_is_safe(self) -> None:
        # Calling restore without takeover should be a no-op, not an error.
        restore_stdout()
        assert is_stdout_taken_over() is False


class TestStdoutRedirection:
    def test_print_goes_to_stderr_after_takeover(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        take_over_stdout()
        try:
            print("hello from a rogue tool")
            sys.stdout.flush()
        finally:
            restore_stdout()
        captured = capsys.readouterr()
        # Stdout should be empty, stderr should carry the print.
        assert captured.out == ""
        assert "hello from a rogue tool" in captured.err

    def test_normal_stdout_after_restore(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        take_over_stdout()
        restore_stdout()
        print("back to normal")
        sys.stdout.flush()
        captured = capsys.readouterr()
        # After restore, stdout works as usual.
        assert "back to normal" in captured.out
        assert captured.err == ""


class TestRawStdoutEscapeHatch:
    def test_write_raw_stdout_bypasses_guard(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        take_over_stdout()
        try:
            # Intentional write — should appear on stdout, not stderr.
            write_raw_stdout("intentional output\n")
        finally:
            restore_stdout()
        captured = capsys.readouterr()
        assert "intentional output" in captured.out
        assert captured.err == ""

    def test_write_raw_stdout_works_without_takeover(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # No-takeover path: just plain stdout write.
        write_raw_stdout("plain write\n")
        captured = capsys.readouterr()
        assert "plain write" in captured.out


class TestErrorHandling:
    def test_takeover_after_replaced_stdout_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If something else has already replaced sys.stdout with a
        non-TextIO object (e.g. a captured-test buffer), takeover must
        refuse rather than corrupt the substitute."""
        # Replace stdout with a bare StringIO — no `.write` chain we own.
        fake = io.StringIO()
        monkeypatch.setattr(sys, "stdout", fake)
        with pytest.raises(OutputGuardError):
            take_over_stdout(strict=True)

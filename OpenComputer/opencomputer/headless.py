"""Headless-mode detection.

Three sources, in priority order:

1. ``force=True`` kwarg passed by the CLI when ``--headless`` is on the command line.
2. ``OPENCOMPUTER_HEADLESS`` env var — truthy values: ``1`` ``true`` ``yes`` ``on``
   (case-insensitive). Any other value (including unset) is falsy.
3. ``sys.stdin.isatty()`` — if no TTY, we infer headless.

Headless is a *display* concept, not a *channel* concept: the agent is
still running, talking to channels (Telegram/Discord/etc.) — it just
shouldn't render Rich Live, ring the terminal bell, or open a
prompt-toolkit picker.
"""
from __future__ import annotations

import os
import sys

_TRUTHY = {"1", "true", "yes", "on"}


def is_headless(*, force: bool = False) -> bool:
    """Return True if the process is running headless (no interactive TTY)."""
    if force:
        return True
    env = os.environ.get("OPENCOMPUTER_HEADLESS", "").strip().lower()
    if env in _TRUTHY:
        return True
    if env and env not in _TRUTHY:
        # Explicit falsy override — even if stdin happens to be a non-TTY,
        # the user said no. Useful for ``OPENCOMPUTER_HEADLESS=0 pytest``.
        return False
    try:
        return not sys.stdin.isatty()
    except (AttributeError, ValueError):
        # ``ValueError: I/O operation on closed file`` happens under some
        # supervisors. Treat as headless — better to be quiet than to crash.
        return True


__all__ = ["is_headless"]

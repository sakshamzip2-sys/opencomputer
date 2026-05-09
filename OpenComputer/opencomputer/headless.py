"""Headless-mode detection + non-interactive output mode.

Three sources for ``is_headless`` detection, in priority order:

1. ``force=True`` kwarg passed by the CLI when ``--headless`` is on the command line.
2. ``OPENCOMPUTER_HEADLESS`` env var — truthy values: ``1`` ``true`` ``yes`` ``on``
   (case-insensitive). Any other value (including unset) is falsy.
3. ``sys.stdin.isatty()`` — if no TTY, we infer headless.

Headless is a *display* concept, not a *channel* concept: the agent is
still running, talking to channels (Telegram/Discord/etc.) — it just
shouldn't render Rich Live, ring the terminal bell, or open a
prompt-toolkit picker.

:class:`OutputMode` (v1.1 plan-1 M2.2, 2026-05-09) controls how the
non-interactive entry points (``oc oneshot``, ``oc chat -q``) emit
their result on stdout — covers the CI / shell-pipeline use case
where the caller wants ``jq``-friendly JSON or per-event NDJSON
instead of the human-readable text.
"""
from __future__ import annotations

import os
import sys
from enum import Enum

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


class OutputMode(str, Enum):
    """Stdout shape for non-interactive ``oc oneshot`` / ``oc chat -q`` runs.

    * :attr:`TEXT` — default; print the assistant's final-message text.
    * :attr:`JSON` — at the end of the run, emit a single JSON object
      summarising the session (session id, turn count, token totals,
      cost, final message, optional error code).
    * :attr:`STREAM_JSON` — newline-delimited JSON. One line per
      ``LLMCallEvent`` in the order they fire (turn-by-turn), then a
      final summary line with ``"event": "summary"``. The file write
      to ``~/.opencomputer/<profile>/llm_events.jsonl`` stays intact —
      stream-json is an *additional* sink, not a replacement.

    Subclassing ``str`` makes the enum members behave as drop-in
    strings so Typer can render them as choices and tests can compare
    via ``mode == "json"``.
    """

    TEXT = "text"
    JSON = "json"
    STREAM_JSON = "stream-json"


def parse_output_mode(value: str) -> OutputMode:
    """Coerce a CLI string to an :class:`OutputMode`.

    Raises :class:`ValueError` with a friendly message when the value
    isn't one of the canonical labels. Centralized so the CLI surface
    + downstream callers + tests all reject the same set of typos.
    """
    try:
        return OutputMode(value)
    except ValueError as exc:
        valid = ", ".join(m.value for m in OutputMode)
        raise ValueError(
            f"unknown --output mode {value!r}; expected one of: {valid}"
        ) from exc


__all__ = ["OutputMode", "is_headless", "parse_output_mode"]

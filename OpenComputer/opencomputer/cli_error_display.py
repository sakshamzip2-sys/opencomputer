"""User-friendly error formatting for the CLI.

Ported from Hermes's ``agent/error_classifier.py:_extract_message`` —
extracts just the human-readable message from provider exceptions
(Anthropic ``BadRequestError``, OpenAI ``BadRequestError``, etc.) instead
of dumping the entire ``{'type':'error', 'error':{'type':...,...}}``
Python dict repr in the user's terminal.

Resolution order (matches Hermes):
  1. ``error.body.error.message`` (Anthropic / OpenAI structured errors)
  2. ``error.body.message`` (some flat error shapes)
  3. ``str(error)`` (catch-all)

Long messages truncated to 500 chars to keep the terminal readable.
"""
from __future__ import annotations

import re
from typing import Any

_MAX_MESSAGE_LEN = 500

# Strip the leading "Error code: NNN - " preamble that Anthropic SDK
# attaches to ``str(BadRequestError)``. The structured message is more
# useful for the user than the redundant preamble.
_PREAMBLE_RE = re.compile(r"^Error code: \d+ - ")


def extract_friendly_message(error: BaseException) -> str:
    """Return the most informative human-readable message from ``error``.

    Walks the ``body`` attribute (Anthropic/OpenAI SDK convention) for a
    structured ``error.message`` field; falls back to stripping the noisy
    "Error code: NNN - " preamble from ``str(error)``. Always truncated
    to at most ``_MAX_MESSAGE_LEN`` chars so the terminal stays readable.
    """
    body = getattr(error, "body", None)
    if isinstance(body, dict):
        error_obj = body.get("error")
        if isinstance(error_obj, dict):
            msg = error_obj.get("message")
            if isinstance(msg, str) and msg.strip():
                return msg.strip()[:_MAX_MESSAGE_LEN]
        msg = body.get("message")
        if isinstance(msg, str) and msg.strip():
            return msg.strip()[:_MAX_MESSAGE_LEN]

    raw = str(error)
    cleaned = _PREAMBLE_RE.sub("", raw, count=1).strip()
    return cleaned[:_MAX_MESSAGE_LEN]


def format_error_for_console(error: BaseException) -> str:
    """Build a Rich-markup error string suited for ``console.print``.

    Format: ``[bold red]✗ <ErrorType>:[/bold red] <friendly message>``
    """
    err_type = type(error).__name__
    msg = extract_friendly_message(error)
    return f"[bold red]✗ {err_type}:[/bold red] {msg}"


def format_provider_error_for_console(error: BaseException) -> str:
    """Same as :func:`format_error_for_console` but with an HTTP-status hint.

    For provider errors (which usually carry a ``status_code`` attribute)
    appends `` (HTTP NNN)`` so the user can quickly distinguish 400 vs 500
    vs 429 without parsing the message body.
    """
    base = format_error_for_console(error)
    status = getattr(error, "status_code", None) or _extract_status_from_str(error)
    if status:
        return f"{base} [dim](HTTP {status})[/dim]"
    return base


def _extract_status_from_str(error: BaseException) -> int | None:
    match = re.match(r"Error code: (\d+)", str(error))
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            return None
    return None


__all__ = [
    "extract_friendly_message",
    "format_error_for_console",
    "format_provider_error_for_console",
]


def _truncate_for_test_visibility(_: Any) -> int:  # pragma: no cover
    """Sentinel — keeps ``_MAX_MESSAGE_LEN`` accessible for tests if reflected."""
    return _MAX_MESSAGE_LEN

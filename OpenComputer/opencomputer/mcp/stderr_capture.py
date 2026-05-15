"""Per-server stderr capture for stdio MCP subprocesses.

mcp-openclaw-port follow-up (Gap B). Default ``stdio_client`` plumbs
the subprocess's stderr to the parent (``sys.stderr``) — chatty MCP
servers spam the user's terminal. This module owns the per-server log
file under ``<profile_home>/logs/mcp/<server>.log``.

Callers (``MCPConnection._owner_lifetime``) call
:func:`open_mcp_stderr_log` to get a writable text-mode file handle,
pass it to ``stdio_client(params, errlog=handle)``, and close the
handle when the connection tears down.

Filename sanitization prevents a maliciously-named server (or one
typo'd with path separators) from escaping the logs directory.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import IO

from opencomputer.agent.config import _home

logger = logging.getLogger("opencomputer.mcp.stderr_capture")

#: Maximum length of the sanitized server name component used in the
#: filename. Generous — most names fit in 32 chars; the cap defends
#: against pathological 10kB names that would blow up the filesystem
#: entry. Matches the OpenClaw 64-char cap with headroom for the
#: ``__`` separators when our naming is ``<plugin>__<server>``.
_MAX_SANITIZED_NAME_LEN: int = 128

#: After sanitization, the name MUST match this regex. We assert it
#: as a defensive post-condition so a sanitizer regression fails loud.
#: Dots are EXCLUDED — a ``..`` pattern looks like a parent-directory
#: traversal at a glance even when our path operations don't interpret
#: it that way. Disallowing dots in the sanitized output is the simplest
#: defence; ``plug.local`` becomes ``plug_local`` (acceptable cost).
SAFE_SERVER_NAME_RE: re.Pattern[str] = re.compile(r"^[A-Za-z0-9_\-]+$")

#: Characters allowed verbatim in a sanitized name. Everything else
#: becomes ``_``.
_SAFE_CHAR_RE: re.Pattern[str] = re.compile(r"[A-Za-z0-9_\-]")


def sanitize_server_name_for_path(name: str) -> str:
    """Return a filesystem-safe form of ``name`` capped at 128 chars.

    Empty inputs become ``_unknown`` so we always have a valid filename.
    Runs of unsafe characters collapse to a single underscore? **No** —
    the implementation maps each unsafe char to ``_`` 1:1 so the
    transformation is reversible at a glance. Tests assert the
    post-condition matches :data:`SAFE_SERVER_NAME_RE`.
    """
    if not name:
        return "_unknown"
    chars: list[str] = []
    for ch in name:
        if _SAFE_CHAR_RE.match(ch):
            chars.append(ch)
        else:
            chars.append("_")
    sanitized = "".join(chars)
    if len(sanitized) > _MAX_SANITIZED_NAME_LEN:
        sanitized = sanitized[:_MAX_SANITIZED_NAME_LEN]
    # Defensive post-condition — should always hold given the loop above.
    assert SAFE_SERVER_NAME_RE.fullmatch(sanitized), (
        f"sanitizer regression: {name!r} → {sanitized!r}"
    )
    return sanitized


def mcp_stderr_log_path(server_name: str) -> Path:
    """Resolve the log path for ``server_name`` (no filesystem side effects)."""
    safe = sanitize_server_name_for_path(server_name)
    return _home() / "logs" / "mcp" / f"{safe}.log"


def open_mcp_stderr_log(server_name: str) -> IO[str]:
    """Open ``<profile>/logs/mcp/<server>.log`` in append mode.

    Creates the parent directory if needed. Returns a text-mode handle
    with line buffering (``buffering=1``) so log entries flush eagerly
    — important since a crashing MCP server's last words land here.

    Callers MUST close the handle when the connection tears down to
    avoid leaking file descriptors.
    """
    target = mcp_stderr_log_path(server_name)
    target.parent.mkdir(parents=True, exist_ok=True)
    logger.debug("opening MCP stderr log: %s", target)
    return open(target, "a", encoding="utf-8", buffering=1)


__all__ = [
    "SAFE_SERVER_NAME_RE",
    "mcp_stderr_log_path",
    "open_mcp_stderr_log",
    "sanitize_server_name_for_path",
]

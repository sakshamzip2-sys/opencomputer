"""MCP-specific log redaction helpers (Gap E).

mcp-openclaw-port follow-up. The central :mod:`opencomputer.security.redact`
module handles GitHub PATs, OpenAI keys, bearer tokens, etc. — and the
LLM-facing path (``MCPTool.execute``) already pipes errors through it.

This module adds a small MCP-flavored layer over the central sweep so
*log-line* output from MCP code paths (``logger.warning(...)``, error
prints) is also redacted, not just the LLM-facing strings. Targets:

* URL query strings containing ``token=`` / ``api_key=`` / ``key=`` /
  ``access_token=`` — values get replaced with ``<REDACTED>``.
* ``x-api-key=...`` / ``X-Api-Key:`` header patterns.
* ``Bearer <token>`` patterns where the central sweep's bearer regex
  doesn't catch the variant (defensive belt-and-braces).

Use anywhere an MCP code path passes potentially-secret-bearing text
to ``logger.<level>`` — e.g. exception messages, headers in spawn
error logs, response bodies surfaced into log messages.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Final

from opencomputer.security.redact import redact_runtime_text

#: Sentinel type for path-like objects so the redacting adapter can
#: catch ``pathlib.Path`` etc. without explicitly importing each.
_PathLike = os.PathLike

#: Query-string params we strip values for. Conservative set — adding
#: a new param here is a one-line change.
_SECRET_QUERY_PARAMS: Final[tuple[str, ...]] = (
    "token",
    "access_token",
    "api_key",
    "apikey",
    "key",
    "auth",
    "password",
    "client_secret",
    "secret",
)

#: Pattern for ``?token=value&other=y`` or ``&token=value`` style
#: query-string secrets. We replace just the value, keeping the param
#: name so log readers know what kind of secret was scrubbed.
_QUERY_SECRET_RE: re.Pattern[str] = re.compile(
    r"(?P<sep>[?&])(?P<key>"
    + "|".join(_SECRET_QUERY_PARAMS)
    + r")=(?P<val>[^&\s]+)",
    flags=re.IGNORECASE,
)

#: Pattern for ``x-api-key=value`` or ``X-Api-Key: value`` log entries.
#: Matches both ``=value`` (query-style) and ``: value`` (header-style)
#: separators.
_HEADER_API_KEY_RE: re.Pattern[str] = re.compile(
    r"(?P<key>x-api-key|x_api_key|api[-_]key)"
    r"\s*[:=]\s*"
    r"(?P<val>[^\s,&]+)",
    flags=re.IGNORECASE,
)

#: Pattern for ``Authorization: Bearer <token>``. The central sweep
#: catches bearer tokens in some forms; this is a defensive add for
#: the variants we've seen in MCP server error output.
_BEARER_RE: re.Pattern[str] = re.compile(
    r"(?P<scheme>Bearer)\s+(?P<val>[A-Za-z0-9._\-]{8,})",
)


def redact_mcp_log_text(text: str) -> str:
    """Run the MCP-flavored redaction sweep over ``text``.

    Pipeline:

    1. Central :func:`redact_runtime_text` — catches all common secret
       shapes (API keys, JWTs, postgres URLs, ssh keys).
    2. URL query-string secrets — replace each ``token=...`` value
       with ``<REDACTED>``.
    3. ``x-api-key`` header patterns.
    4. ``Bearer <token>`` patterns.

    Returns the redacted string. Idempotent + safe on empty input.
    """
    if not text:
        return text
    out = redact_runtime_text(text)
    out = _QUERY_SECRET_RE.sub(
        lambda m: f"{m.group('sep')}{m.group('key')}=<REDACTED>",
        out,
    )
    out = _HEADER_API_KEY_RE.sub(
        lambda m: f"{m.group('key')}=<REDACTED>",
        out,
    )
    out = _BEARER_RE.sub(
        lambda m: f"{m.group('scheme')} <REDACTED>",
        out,
    )
    return out


class _RedactingLoggerAdapter:
    """Thin wrapper that runs every ``%s`` arg through redaction.

    Usage: ``log = redacting(logger)`` then ``log.warning("msg: %s", exc)``
    behaves like the underlying logger but the rendered message has all
    secret-bearing patterns stripped.

    Why this layer (instead of redacting at log-handler / formatter
    level) — formatter-level redaction would catch every log site in the
    project, which is too broad. This adapter is opt-in per-module:
    MCP code uses it; the rest of OC keeps the central
    ``redact_runtime_text`` for LLM-facing strings.
    """

    __slots__ = ("_inner",)

    def __init__(self, inner: logging.Logger) -> None:
        self._inner = inner

    @staticmethod
    def _redact_args(args: tuple[Any, ...]) -> tuple[Any, ...]:
        """Redact every arg that ``logging``'s ``%s`` formatter would
        stringify. Exception objects, Path objects, anything whose
        ``__str__`` could carry secret-bearing text — all get
        normalised to str + redacted. Numeric / bool / None args pass
        through unchanged (they can't carry secrets and need to
        preserve their type for ``%d`` / ``%r`` style format specs).
        """
        out: list[Any] = []
        for a in args:
            if isinstance(a, (str, BaseException, _PathLike)):
                out.append(redact_mcp_log_text(str(a)))
            else:
                out.append(a)
        return tuple(out)

    def debug(self, msg: str, *args: Any, **kw: Any) -> None:
        self._inner.debug(msg, *self._redact_args(args), **kw)

    def info(self, msg: str, *args: Any, **kw: Any) -> None:
        self._inner.info(msg, *self._redact_args(args), **kw)

    def warning(self, msg: str, *args: Any, **kw: Any) -> None:
        self._inner.warning(msg, *self._redact_args(args), **kw)

    def error(self, msg: str, *args: Any, **kw: Any) -> None:
        self._inner.error(msg, *self._redact_args(args), **kw)

    def exception(self, msg: str, *args: Any, **kw: Any) -> None:
        self._inner.exception(msg, *self._redact_args(args), **kw)

    def log(self, level: int, msg: str, *args: Any, **kw: Any) -> None:
        self._inner.log(level, msg, *self._redact_args(args), **kw)


def redacting(logger: logging.Logger) -> _RedactingLoggerAdapter:
    """Wrap ``logger`` so every ``%s`` arg gets redacted on log."""
    return _RedactingLoggerAdapter(logger)


__all__ = [
    "redact_mcp_log_text",
    "redacting",
]

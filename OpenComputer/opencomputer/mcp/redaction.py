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

import re
from typing import Final

from opencomputer.security.redact import redact_runtime_text

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


__all__ = [
    "redact_mcp_log_text",
]

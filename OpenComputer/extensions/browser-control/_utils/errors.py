"""Single typed error class for the browser-port surface.

Server-side throws subclasses (see future server/ + client/ modules); the
client side catches the base. 429 is mapped to a friendlier static hint
because rate-limit feedback is one of the few status codes the agent
should react to specially. Non-429 messages come from the server body
verbatim — we never reflect upstream text into the agent for 429s
(prompt-injection surface).
"""

from __future__ import annotations

from typing import Any

_RATE_LIMIT_HINT = (
    "Browser service rate limit reached. "
    "Wait for the current session to complete, or retry later."
)


class BrowserServiceError(Exception):
    """The control service returned an error or could not be reached."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code

    @classmethod
    def from_response(cls, status: int, body: dict[str, Any] | None) -> BrowserServiceError:
        body = body or {}
        nested = body.get("error") if isinstance(body.get("error"), dict) else None
        message: str | None = None
        code: str | None = None
        if isinstance(nested, dict):
            raw_msg = nested.get("message")
            raw_code = nested.get("code")
            if isinstance(raw_msg, str):
                message = raw_msg
            if isinstance(raw_code, str):
                code = raw_code
        if status == 429:
            return cls(_RATE_LIMIT_HINT, status=status, code=code)
        if message is None:
            top_msg = body.get("message")
            if isinstance(top_msg, str):
                message = top_msg
        if not message:
            message = f"HTTP {status}"
        return cls(message, status=status, code=code)

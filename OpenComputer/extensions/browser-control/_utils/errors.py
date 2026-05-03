"""Single typed error class for the browser-port surface.

Server-side throws subclasses (see future server/ + client/ modules); the
client side catches the base. 429 is mapped to a friendlier static hint
because rate-limit feedback is one of the few status codes the agent
should react to specially. Non-429 messages come from the server body
verbatim — we never reflect upstream text into the agent for 429s
(prompt-injection surface).

Wave 4 (adapter promotion) adds 5 typed error subclasses for the
``adapter-runner`` surface. Each carries:
  - ``code``  — short string id surfaced via ``ToolResult.content`` /
                propagated as the ``code`` field on errors.
  - ``exit_code`` — POSIX ``sysexits.h`` mapping, mirrors OpenCLI's
                   error → exit-code contract from the LearnX BUILD.md.
"""

from __future__ import annotations

from typing import Any, ClassVar

_RATE_LIMIT_HINT = (
    "Browser service rate limit reached. "
    "Wait for the current session to complete, or retry later."
)


class BrowserServiceError(Exception):
    """The control service returned an error or could not be reached."""

    #: Short string code surfaced via the ``code`` property. Subclasses
    #: override this ClassVar to set a stable per-class default; an
    #: instance can override either via the ``code=`` constructor kwarg.
    _default_code: ClassVar[str | None] = None

    #: POSIX sysexits.h code for adapter / CLI compositions. Subclasses
    #: override; default 1 (EX_GENERAL).
    exit_code: ClassVar[int] = 1

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        # Instance value takes priority; falls back to the class default
        # via the ``code`` property below. Stored on a private name so
        # the public ``code`` accessor can be a property without
        # clashing with the ClassVar.
        self._instance_code = code

    @property
    def code(self) -> str | None:
        """Resolved code: instance override (constructor) → class default → None."""
        if self._instance_code is not None:
            return self._instance_code
        return type(self)._default_code

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


# ─── Wave 4 adapter-runner error taxonomy ──────────────────────────────


class AuthRequiredError(BrowserServiceError):
    """Adapter detected the user isn't logged in / auth expired.

    Map to ``EX_NOPERM`` (77). Triggered when a fetch returns 401/403,
    or when the adapter's own logic finds a "log in" page redirect.
    """

    _default_code: ClassVar[str | None] = "auth_required"
    exit_code: ClassVar[int] = 77


class AdapterEmptyResultError(BrowserServiceError):
    """Adapter ran without error but returned no rows.

    Map to ``EX_NOINPUT`` (66). Useful as a distinct signal when the
    adapter completed cleanly but the server's response was empty.
    """

    _default_code: ClassVar[str | None] = "empty_result"
    exit_code: ClassVar[int] = 66


class AdapterTimeoutError(BrowserServiceError):
    """Adapter exceeded its time budget. Map to ``EX_TEMPFAIL`` (75)."""

    _default_code: ClassVar[str | None] = "timeout"
    exit_code: ClassVar[int] = 75


class AdapterConfigError(BrowserServiceError):
    """Adapter or site memory is misconfigured.

    Map to ``EX_CONFIG`` (78). Raised when ``@adapter`` metadata is
    invalid (duplicate site/name, missing required field, unknown
    ``Strategy``), or when a verify fixture is malformed.
    """

    _default_code: ClassVar[str | None] = "config"
    exit_code: ClassVar[int] = 78


class AdapterNotFoundError(BrowserServiceError):
    """Adapter file exists but no @adapter-decorated function found.

    Distinct from ``AdapterConfigError`` because the file may be
    syntactically fine but simply missing the decorator. ``exit_code``
    inherits the ``BrowserServiceError`` default (1).
    """

    _default_code: ClassVar[str | None] = "not_found"


__all__ = [
    "BrowserServiceError",
    "AuthRequiredError",
    "AdapterEmptyResultError",
    "AdapterTimeoutError",
    "AdapterConfigError",
    "AdapterNotFoundError",
]

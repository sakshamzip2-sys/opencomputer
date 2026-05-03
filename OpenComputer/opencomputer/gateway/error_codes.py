"""Typed wire error codes - programmable categories.

Sub-project G (openclaw-parity) Task 9. ``WireResponse.error`` is opaque
text. Wire clients (TUI, IDE bridges) cannot ``match`` on errors. This
enum gives them a programmable category that round-trips through JSON
as a stable snake_case string.

Mirrors openclaw ``error-codes.ts`` shape from
``sources/openclaw-2026.4.23/src/gateway/protocol/schema/error-codes.ts``.

Add new codes to the END of the enum; never renumber. Existing wire
callers tolerate unknown codes gracefully (treat as INTERNAL_ERROR).
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["ErrorCode"]


class ErrorCode(StrEnum):
    """Programmable error categories for ``WireResponse.code``.

    The string value is the wire shape - lowercase snake_case. Use
    ``.value`` when serializing if you want a pure ``str``; the enum
    itself compares equal to its value for client convenience.
    """

    # Plugin lifecycle
    PLUGIN_NOT_FOUND = "plugin_not_found"
    PLUGIN_INCOMPATIBLE = "plugin_incompatible"

    # Auth / provider
    PROVIDER_AUTH_FAILED = "provider_auth_failed"

    # Tools / consent
    TOOL_DENIED = "tool_denied"
    CONSENT_BLOCKED = "consent_blocked"

    # Wire-protocol layer
    METHOD_NOT_FOUND = "method_not_found"
    INVALID_PARAMS = "invalid_params"

    # Reliability
    INTERNAL_ERROR = "internal_error"
    RATE_LIMITED = "rate_limited"

    # Session
    SESSION_NOT_FOUND = "session_not_found"

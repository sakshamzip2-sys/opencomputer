"""scope_check — pure predicate for tool+path/command permission decisions."""

from __future__ import annotations

from .default_scopes import (
    DEFAULT_BASH_DENY,
    DEFAULT_DENY,
    is_in_cwd,
    matches_any,
)

SCOPED_FILE_TOOLS = frozenset({"Edit", "MultiEdit", "Write"})
SCOPED_BASH_TOOLS = frozenset({"Bash"})


def is_allowed(tool_name: str, value: str) -> tuple[bool, str | None]:
    """Return (allowed, reason_if_denied).

    `value` is a file path for file tools, or the raw command for Bash.
    """
    if tool_name in SCOPED_FILE_TOOLS:
        if matches_any(value, DEFAULT_DENY):
            return False, f"path {value!r} is on the default deny list"
        if value.startswith("/") and not is_in_cwd(value):
            return False, f"path {value!r} is outside the current workspace"
        return True, None

    if tool_name in SCOPED_BASH_TOOLS:
        if matches_any(value, DEFAULT_BASH_DENY):
            return False, f"bash command {value!r} is on the default deny list"
        return True, None

    # Non-scoped tools: always allowed by this hook.
    return True, None


__all__ = ["is_allowed", "SCOPED_FILE_TOOLS", "SCOPED_BASH_TOOLS"]

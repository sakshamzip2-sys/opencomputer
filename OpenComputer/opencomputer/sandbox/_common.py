"""Shared helpers for sandbox strategy implementations.

Internal — not part of the public SDK. Concrete strategies in this
subpackage import from here; nothing outside ``opencomputer/sandbox/``
should depend on this module.
"""

from __future__ import annotations

import os
from typing import Any

from plugin_sdk.sandbox import SandboxConfig

# Sentinel stderr emitted when the wall-clock cap (config.cpu_seconds_limit)
# is exceeded. Tests assert on this exact string — keep stable.
TIMEOUT_STDERR = "[sandbox timeout]"

# Exit code returned alongside ``TIMEOUT_STDERR``. Negative so it can't
# collide with a real subprocess exit code (those are 0..255 on POSIX).
TIMEOUT_EXIT_CODE = -1


def filtered_env(config: SandboxConfig, *, extras: dict[str, str] | None = None) -> dict[str, str]:
    """Build an env dict containing only keys in ``config.allowed_env_vars``.

    ``extras`` (if supplied) is overlaid AFTER the allowlist filter — used
    by strategies that need to inject ``TMPDIR`` or similar containment
    variables that aren't user-declared.
    """
    out: dict[str, str] = {}
    parent = os.environ
    for key in config.allowed_env_vars:
        if key in parent:
            out[key] = parent[key]
    if extras:
        out.update(extras)
    return out


def decode_stream(data: Any) -> str:
    """Best-effort UTF-8 decode for subprocess stdout/stderr.

    ``errors='replace'`` so a binary blob doesn't crash the caller. ``None``
    + ``b""`` both return ``""`` for symmetry.
    """
    if not data:
        return ""
    if isinstance(data, str):
        return data
    if isinstance(data, (bytes, bytearray)):
        return data.decode("utf-8", errors="replace")
    return str(data)

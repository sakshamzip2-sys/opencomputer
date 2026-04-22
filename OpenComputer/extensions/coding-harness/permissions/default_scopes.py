"""Default allow/deny globs for destructive file tools.

DENY always wins over ALLOW. Paths outside cwd are also blocked by default.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path

#: Paths never touched by the harness.
DEFAULT_DENY: tuple[str, ...] = (
    "/etc/*",
    "/etc/**",
    "/sys/*",
    "/sys/**",
    "/proc/*",
    "/proc/**",
    "/boot/*",
    "/boot/**",
    "/dev/*",
    "/dev/**",
    "/var/log/*",
    "/var/log/**",
    "/root/*",
    "/root/**",
    "/Library/Application Support/*",
    "/Library/Application Support/**",
)

#: Bash commands we never let run.
DEFAULT_BASH_DENY: tuple[str, ...] = (
    "rm -rf /",
    "rm -rf /*",
    "rm -rf ~",
    ":(){ :|:& };:",  # fork bomb
    "mkfs*",
    "dd if=*",
    "chmod 777 /*",
    "chown -R root:root /*",
)


def matches_any(value: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatch(value, pat) for pat in patterns)


def is_in_cwd(path_str: str) -> bool:
    try:
        p = Path(path_str).resolve()
        return str(p).startswith(str(Path.cwd().resolve()))
    except Exception:
        return False


__all__ = [
    "DEFAULT_DENY",
    "DEFAULT_BASH_DENY",
    "matches_any",
    "is_in_cwd",
]

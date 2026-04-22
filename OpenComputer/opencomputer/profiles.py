"""Profile discovery, validation, and sticky-default file management.

Adds multi-profile support so `opencomputer -p coder` switches to a
separate MEMORY.md/USER.md/config.yaml set at ~/.opencomputer/profiles/coder/.
The default profile lives at the root (~/.opencomputer/) for zero-migration
of existing users.

Paired with the _apply_profile_override() in opencomputer/cli.py which sets
OPENCOMPUTER_HOME before any opencomputer.* import, so _home() resolves to
the correct profile directory everywhere downstream.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

# Reserved names: either structural (default = root) or would collide with
# subdirectories of the root (profiles/, presets/, wrappers/, skills/, plugins/).
_RESERVED = frozenset({"default", "presets", "wrappers", "plugins", "profiles", "skills"})


class ProfileNameError(ValueError):
    """Raised when a profile name fails validation."""


def validate_profile_name(name: str) -> None:
    """Raise ProfileNameError if *name* is not a valid profile identifier."""
    if not name:
        raise ProfileNameError("profile name cannot be empty")
    if name in _RESERVED:
        raise ProfileNameError(f"'{name}' is reserved (reserved names: {sorted(_RESERVED)})")
    if not _NAME_RE.match(name):
        raise ProfileNameError(
            f"invalid profile name '{name}'. Must match [a-z0-9][a-z0-9_-]* "
            "(lowercase alphanumeric; underscores and hyphens allowed after first char)"
        )


def get_default_root() -> Path:
    """Return the always-present profile root (~/.opencomputer/).

    Respects OPENCOMPUTER_HOME_ROOT for testing; this is NOT the same as
    OPENCOMPUTER_HOME (which is set dynamically by _apply_profile_override
    to point at the active profile's directory).
    """
    override = os.environ.get("OPENCOMPUTER_HOME_ROOT")
    if override:
        return Path(override)
    return Path.home() / ".opencomputer"


def get_profile_dir(name: str | None) -> Path:
    """Return the directory for a profile.

    - None or "default" → the root (~/.opencomputer/)
    - Named → ~/.opencomputer/profiles/<name>/
    """
    if name is None or name == "default":
        return get_default_root()
    validate_profile_name(name)
    return get_default_root() / "profiles" / name


def list_profiles() -> list[str]:
    """Return sorted names of all profiles under ~/.opencomputer/profiles/.

    Only returns subdirectories — skips stray files.
    """
    root = get_default_root() / "profiles"
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())


def read_active_profile() -> str | None:
    """Return the sticky active profile name, or None for default.

    None is returned when:
      - ~/.opencomputer/active_profile file is missing.
      - File is empty or just whitespace.
      - File contains "default".
      - File contains an invalid name (treated as corrupt; fall back to default).
    """
    path = get_default_root() / "active_profile"
    if not path.exists():
        return None
    try:
        name = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not name or name == "default":
        return None
    try:
        validate_profile_name(name)
    except ProfileNameError:
        return None  # corrupt file; treat as default
    return name


def write_active_profile(name: str | None) -> None:
    """Persist the sticky active profile.

    Passing None or "default" removes the active_profile file (reverts to default).
    Otherwise validates *name* then writes it.
    """
    path = get_default_root() / "active_profile"
    if name is None or name == "default":
        path.unlink(missing_ok=True)
        return
    validate_profile_name(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(name + "\n", encoding="utf-8")


__all__ = [
    "ProfileNameError",
    "validate_profile_name",
    "get_default_root",
    "get_profile_dir",
    "list_profiles",
    "read_active_profile",
    "write_active_profile",
]

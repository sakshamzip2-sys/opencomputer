"""Profile discovery, validation, and sticky-default file management.

Adds multi-profile support so `opencomputer -p coder` switches to a
separate MEMORY.md/USER.md/config.yaml set at ~/.opencomputer/profiles/coder/.
The default profile lives at the root (~/.opencomputer/) for zero-migration
of existing users.

Paired with :func:`opencomputer.cli._apply_profile_override`, which runs
inside ``main()`` to strip the ``-p`` / ``--profile`` flag from argv and
set ``OPENCOMPUTER_HOME``. That env var is consulted lazily by
:func:`opencomputer.agent.config._home` on every call (no module-level
caching), so any code path that resolves paths AFTER ``main()`` has
called the override sees the correct profile directory — whether it
runs during Typer command dispatch, inside an agent loop, or from a
subprocess that inherits the env.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

# Keep a module-level reference so ruff / isort autofix cannot remove the
# `shutil` import when it scans only the top of the file.
_shutil_ref = shutil

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


class ProfileExistsError(ValueError):
    """Raised when creating or renaming into a profile name that already exists."""


class ProfileNotFoundError(ValueError):
    """Raised when an operation targets a profile that does not exist."""


def create_profile(
    name: str,
    *,
    clone_from: str | None = None,
    clone_all: bool = False,
) -> Path:
    """Create a new profile directory.

    - ``clone_from``: source profile name. If set and ``clone_all`` is False,
      copies only ``config.yaml`` (and ``profile.yaml`` if present).
    - ``clone_all``: full recursive copy of the source directory.

    Raises ``ProfileExistsError`` if a profile with this name already exists.
    Raises ``ProfileNotFoundError`` if ``clone_from`` is set but the source
    doesn't exist. Raises ``ProfileNameError`` for invalid names (including
    ``"default"`` and other reserved names).
    """
    validate_profile_name(name)
    dest = get_profile_dir(name)
    if dest.exists():
        raise ProfileExistsError(f"profile '{name}' already exists at {dest}")

    if clone_from is not None and clone_all:
        src = get_profile_dir(clone_from)
        if not src.is_dir():
            raise ProfileNotFoundError(f"source profile '{clone_from}' not found at {src}")
        # copytree creates dest and parents
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dest)
        return dest

    dest.mkdir(parents=True, exist_ok=False)
    if clone_from is not None:
        src = get_profile_dir(clone_from)
        if not src.is_dir():
            # Roll back the empty dir we just created so the state is clean.
            try:
                dest.rmdir()
            except OSError:
                pass
            raise ProfileNotFoundError(f"source profile '{clone_from}' not found at {src}")
        for fname in ("config.yaml", "profile.yaml"):
            src_file = src / fname
            if src_file.exists():
                shutil.copy2(src_file, dest / fname)
    return dest


def delete_profile(name: str) -> None:
    """Remove a profile directory and clear the sticky file if it was active.

    Refuses to delete the ``default`` profile (rejected by
    ``validate_profile_name``). Raises ``ProfileNotFoundError`` if the
    profile does not exist.
    """
    validate_profile_name(name)  # rejects "default" and other reserved
    target = get_profile_dir(name)
    if not target.is_dir():
        raise ProfileNotFoundError(f"profile '{name}' not found at {target}")
    # Clear sticky if the deleted profile was active
    if read_active_profile() == name:
        write_active_profile(None)
    shutil.rmtree(target)


def rename_profile(old: str, new: str) -> Path:
    """Move a profile directory from ``old`` to ``new`` name.

    Updates the sticky ``active_profile`` file if ``old`` was the active
    profile. The caller is responsible for printing any user-facing
    continuity warning (Honcho etc.) — this helper only moves the dir.

    Returns the new path. Raises ``ProfileNameError``,
    ``ProfileNotFoundError``, or ``ProfileExistsError``.
    """
    validate_profile_name(old)
    validate_profile_name(new)
    src = get_profile_dir(old)
    dest = get_profile_dir(new)
    if not src.is_dir():
        raise ProfileNotFoundError(f"profile '{old}' not found at {src}")
    if dest.exists():
        raise ProfileExistsError(f"profile '{new}' already exists at {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dest))
    if read_active_profile() == old:
        write_active_profile(new)
    return dest


__all__ = [
    "ProfileNameError",
    "ProfileExistsError",
    "ProfileNotFoundError",
    "validate_profile_name",
    "get_default_root",
    "get_profile_dir",
    "list_profiles",
    "read_active_profile",
    "write_active_profile",
    "create_profile",
    "delete_profile",
    "rename_profile",
]

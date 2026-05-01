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

Sub-project C additions:

- Per-profile ``home/`` subdirectory (C1). Subprocesses spawned by the
  agent (BashTool, gateway, wire) get ``HOME=<profile>/home/`` + matching
  ``XDG_CONFIG_HOME`` / ``XDG_DATA_HOME`` so git / ssh / npm tool configs
  are isolated per profile. Closes the credential-leak weakness raised
  in the Sub-project A adversarial review.
- ``~/.local/bin/<name>`` wrapper script (C2) for one-word profile
  invocation (``coder chat`` → ``opencomputer -p coder chat``).
- Per-profile ``SOUL.md`` (C3) seeded at profile create, threaded into
  the agent's frozen base prompt as a ``## Profile identity`` section.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import sys
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


def real_user_home() -> Path:
    """Return the user's real home directory, immune to $HOME mutation.

    `_apply_profile_override` sets HOME to <profile>/home/ for
    subprocess credential isolation. After that, Path.home() returns
    the profile-scoped path, not the user's actual home. That breaks
    every caller that expects ~/.opencomputer/ to resolve to the real
    ~/.opencomputer/ (active_profile lookup, profile path resolution,
    etc.) — instead they get
    ~/.opencomputer/profiles/<name>/home/.opencomputer/.

    Public API as of 2026-05-01 — used directly by callers that need
    the user's REAL home for non-.opencomputer paths (snapshot
    destinations, the ``oc`` binstub on $PATH, the ``user_home``
    Jinja variable in the system prompt, the ``~/...`` path-display
    anchor in subdirectory hints).

    `pwd.getpwuid()` reads /etc/passwd and ignores the HOME env var,
    so it returns the canonical home regardless. On Windows (no `pwd`
    module) we fall back to Path.home() — Windows isn't subject to
    the same HOME-mutation pattern.
    """
    try:
        import pwd  # POSIX-only
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (ImportError, KeyError):
        return Path.home()


# Backwards-compatible private alias — kept temporarily for any external
# consumer that imported the underscored name. New code should use
# :func:`real_user_home`.
_real_user_home = real_user_home


def get_default_root() -> Path:
    """Return the always-present profile root (~/.opencomputer/).

    Respects OPENCOMPUTER_HOME_ROOT for testing; this is NOT the same as
    OPENCOMPUTER_HOME (which is set dynamically by _apply_profile_override
    to point at the active profile's directory).

    Uses the real user home (via pwd) rather than Path.home() so
    the result is stable even after `_apply_profile_override` mutates
    $HOME for subprocess scoping. Without this, calling `profile use`
    from within an already-active profile would resolve paths relative
    to <profile>/home/ and produce nested nonsense like
    ~/.opencomputer/profiles/coding/home/.opencomputer/profiles/coding.
    """
    override = os.environ.get("OPENCOMPUTER_HOME_ROOT")
    if override:
        return Path(override)
    return real_user_home() / ".opencomputer"


def get_profile_dir(name: str | None) -> Path:
    """Return the directory for a profile.

    - None or "default" → the root (~/.opencomputer/)
    - Named → ~/.opencomputer/profiles/<name>/
    """
    if name is None or name == "default":
        return get_default_root()
    validate_profile_name(name)
    return get_default_root() / "profiles" / name


def profile_home_dir(name: str) -> Path:
    """Return the per-profile ``home/`` subdirectory, creating on demand.

    This is the filesystem sandbox that subprocesses spawned by the agent
    (BashTool, gateway, wire, etc.) see as ``$HOME`` when the profile is
    active. Isolating HOME means git/ssh/npm/etc. write their credentials
    and caches under ``~/.opencomputer/profiles/<name>/home/`` instead of
    the user's real home — profile-scoped credential isolation.

    Calling this on the ``default`` profile returns ``<root>/home/`` but
    the default profile intentionally never activates env scoping
    (see :func:`scope_subprocess_env`), so nothing actually uses it.
    """
    target = get_profile_dir(name) / "home"
    target.mkdir(parents=True, exist_ok=True)
    return target


def scope_subprocess_env(
    env: dict[str, str] | None = None,
    *,
    profile: str | None = None,
) -> dict[str, str]:
    """Return an env dict with HOME/XDG_* scoped to a profile's home/.

    - ``env=None`` → start from a copy of ``os.environ``.
    - ``profile`` selects which profile to scope to. ``None`` (the default)
      falls back to :func:`read_active_profile` — the sticky active
      profile. Pass an explicit name to scope without mutating sticky
      state.
    - If the resolved profile is ``None`` or ``"default"`` (no named
      profile), the env is returned unchanged. The default profile
      deliberately does NOT get scoped — it uses the user's real HOME
      so existing tool configs keep working.
    - Otherwise sets HOME, XDG_CONFIG_HOME, XDG_DATA_HOME to the profile's
      ``home/`` subdir and ``home/.config`` / ``home/.local/share``
      respectively. Other env keys are preserved.

    Safe to call at any point after :func:`_apply_profile_override` has
    set the sticky active profile (or the test equivalent).
    """
    env = os.environ.copy() if env is None else dict(env)

    target = profile if profile is not None else read_active_profile()
    if target is None or target == "default":
        return env

    try:
        home = profile_home_dir(target)
    except ProfileNameError:
        return env

    env["HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    env["XDG_DATA_HOME"] = str(home / ".local" / "share")
    return env


def wrapper_path(name: str) -> Path:
    """Path to the ``~/.local/bin/<name>`` wrapper script for a profile.

    ``~/.local/bin/`` is the conventional user-bin location on Linux /
    macOS and is typically already on ``$PATH`` via ``.profile`` /
    ``.zprofile`` / systemd user units. The wrapper invokes
    ``opencomputer -p <name> "$@"`` so the user can type ``coder chat``
    instead of ``opencomputer -p coder chat``.

    Uses :func:`real_user_home` (HOME-mutation-immune) — the user's
    ``$PATH`` is global, not profile-scoped, so the wrapper must land
    in their real ``~/.local/bin/`` regardless of the active profile.
    """
    return real_user_home() / ".local" / "bin" / name


def _maybe_write_wrapper(name: str) -> None:
    """Write ``~/.local/bin/<name>`` wrapper if missing. Skip on Windows.

    Idempotent — never overwrites an existing file. Failures
    (permission denied on ``~/.local/bin/``, etc.) are logged as
    warnings, never raised: profile creation must not block on a
    missing ``~/.local`` directory.
    """
    log = logging.getLogger("opencomputer.profiles")
    if sys.platform.startswith("win") or os.name == "nt":
        log.info("Wrapper script skipped on Windows (unsupported).")
        return

    target = wrapper_path(name)
    if target.exists():
        log.info("Wrapper %s already exists — skipped.", target)
        return

    script = f"""#!/bin/bash
exec opencomputer -p {name} "$@"
"""
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(script, encoding="utf-8")
        target.chmod(0o755)
    except OSError as e:
        log.warning("Could not write wrapper %s: %s", target, e)


def _maybe_remove_wrapper(name: str) -> None:
    """Best-effort removal of ``~/.local/bin/<name>`` — silent on missing."""
    target = wrapper_path(name)
    try:
        target.unlink(missing_ok=True)
    except OSError:
        pass


def _maybe_write_soul_md(name: str) -> None:
    """Seed ``<profile>/SOUL.md`` with a default personality prompt.

    Idempotent — if the file already exists, leave it alone. Users can
    edit ``SOUL.md`` to set per-profile tone/identity; mid-session edits
    do NOT re-freeze the base prompt (that would defeat the prefix-cache
    invariant the agent loop depends on — a new session picks it up).
    """
    log = logging.getLogger("opencomputer.profiles")
    target = get_profile_dir(name) / "SOUL.md"
    if target.exists():
        return

    content = f"""# SOUL — {name}'s personality

You are {name}. When introducing yourself or when context warrants,
briefly reflect this identity. Adjust tone/style to match the profile's
purpose as the user describes it.
"""
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    except OSError as e:
        log.warning("Could not seed SOUL.md %s: %s", target, e)




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
        _post_create_artifacts(name)
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
    _post_create_artifacts(name)
    return dest


def _post_create_artifacts(name: str) -> None:
    """Emit per-profile artifacts after the profile directory exists.

    Separated so both the ``clone_all`` path and the fresh-create path
    produce the same side-effects: C1 (``home/`` subdir), C2 (wrapper
    script), and C3 (``SOUL.md``). Artifacts already present (e.g. from
    a full clone) are left untouched by ``_maybe_*`` helpers.
    """
    # C1 — home/ subdir
    profile_home_dir(name)
    # C2 — wrapper script
    _maybe_write_wrapper(name)
    # C3 — SOUL.md default seed
    _maybe_write_soul_md(name)


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
    # C2 — remove the ~/.local/bin/<name> wrapper script if we wrote one.
    # Silent on missing: the user may have deleted it manually, or Windows
    # skipped it on create.
    _maybe_remove_wrapper(name)


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
    "profile_home_dir",
    "scope_subprocess_env",
    "wrapper_path",
    "list_profiles",
    "read_active_profile",
    "write_active_profile",
    "create_profile",
    "delete_profile",
    "rename_profile",
]

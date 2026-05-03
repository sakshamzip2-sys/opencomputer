"""Helpers shared across service backends.

resolve_executable: locate the ``oc`` shim by trying ``shutil.which`` first,
then a known list of fallbacks (Homebrew, pipx, pyenv, sys.executable's dir).
"""
from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path

_FALLBACK_PATHS: list[Path] = [
    Path("/opt/homebrew/bin/oc"),
    Path("/opt/homebrew/bin/opencomputer"),
    Path.home() / ".local" / "bin" / "oc",
    Path.home() / ".local" / "bin" / "opencomputer",
    Path.home() / ".pyenv" / "shims" / "oc",
    Path.home() / ".pyenv" / "shims" / "opencomputer",
    Path(sys.executable).parent / "oc",
    Path(sys.executable).parent / "opencomputer",
]

_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def resolve_executable() -> str:
    """Locate the oc/opencomputer executable. Returns absolute path string.

    Search order: ``OC_EXECUTABLE`` env var → ``shutil.which("oc")`` →
    ``shutil.which("opencomputer")`` → ``_FALLBACK_PATHS`` (Homebrew,
    ~/.local/bin, pyenv shims, sys.executable dir).
    """
    override = os.environ.get("OC_EXECUTABLE")
    if override and Path(override).exists():
        return override
    for name in ("oc", "opencomputer"):
        found = shutil.which(name)
        if found:
            return found
    for candidate in _FALLBACK_PATHS:
        if candidate.exists():
            return str(candidate)
    raise RuntimeError(
        "could not find oc executable. Tried: $PATH, "
        "/opt/homebrew/bin, ~/.local/bin, ~/.pyenv/shims, "
        f"{Path(sys.executable).parent}. "
        "Set OC_EXECUTABLE env var to override.",
    )


def _validate_profile(profile: str) -> str:
    """Reject profile names with path-traversal or shell-metachar potential."""
    if not _PROFILE_NAME_RE.match(profile):
        raise ValueError(
            f"invalid profile name {profile!r}: must match {_PROFILE_NAME_RE.pattern}",
        )
    return profile


def workdir(profile: str) -> Path:
    """Return the per-profile workdir, creating it if absent.

    Defaults to ``~/.opencomputer/<profile>``. Profile name is validated
    against a strict allowlist to prevent path-traversal abuse.
    """
    _validate_profile(profile)
    if sys.platform.startswith("win"):
        base = Path(os.environ.get("USERPROFILE", str(Path.home())))
    else:
        base = Path(os.environ.get("HOME", str(Path.home())))
    wd = base / ".opencomputer" / profile
    wd.mkdir(parents=True, exist_ok=True)
    return wd


def log_paths(profile: str) -> tuple[Path, Path]:
    """Return (stdout_log, stderr_log) paths for the gateway service.

    Creates the parent ``logs/`` dir if absent.
    """
    base = workdir(profile) / "logs"
    base.mkdir(parents=True, exist_ok=True)
    return (base / "gateway.stdout.log", base / "gateway.stderr.log")


def tail_lines(path: Path, n: int) -> list[str]:
    """Return the last ``n`` lines of ``path``. Empty list if file missing."""
    if not path.exists():
        return []
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return []
    lines = text.splitlines()
    return lines[-n:] if n > 0 else []


__all__ = [
    "log_paths",
    "resolve_executable",
    "tail_lines",
    "workdir",
]

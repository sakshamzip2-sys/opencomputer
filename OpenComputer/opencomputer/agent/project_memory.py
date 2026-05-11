"""Per-project memory store (CC §8).

Spec: docs/OC-FROM-CLAUDE-CODE.md §8.

A small, self-contained module that provides per-project memory files
at ``<profile_home>/projects/<id>/memory.md``. The id is derived from
the project root (preferring the git remote URL when available so the
same project resolves consistently across clones; falling back to the
canonical resolved absolute path).

Distinct from:

  - ``MemoryManager.declarative_path`` — global MEMORY.md (cross-project)
  - ``opencomputer/agent/instructions_hierarchy.py`` — per-directory
    CLAUDE.md / OPENCOMPUTER.md (instruction text, not learnings)

The per-project memory file is the right home for "facts I learned
working in THIS project" — preferred tools, common commands,
architecture notes, gotchas — that don't generalise to other repos.

Design choices:

  - **Stable id**: SHA-256 of the project's identifying string (git
    remote URL or canonical path). Hex-encoded, first 16 chars =
    8-byte prefix. Stable across runs without leaking path info into
    on-disk dir names (acceptable security trade-off; the hex prefix
    is enough to disambiguate within a user's profile).
  - **Lazy creation**: the directory tree is only created on first
    write — readers see "" until something is recorded.
  - **No locking**: append-only writes; a concurrent write race
    would interleave bytes but not corrupt the file (POSIX
    O_APPEND guarantees atomic-per-write). The file format is
    line-oriented markdown so partial writes self-heal at the next
    read.
  - **No truncation**: per-project memory should grow as the agent
    learns. A cap belongs in a separate compaction tool if the file
    ever gets unmanageable.

Safe by default — every operation handles missing dirs, unwritable
paths, and OSError without raising into callers.
"""

from __future__ import annotations

import hashlib
import logging
import os
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

_LOG = logging.getLogger(__name__)

#: Length of the hex prefix used as the project directory name. 16
#: hex chars = 8 bytes = ~2^64 search space — collision-free for any
#: realistic per-user project count.
_PROJECT_ID_HEX_LEN: int = 16


@dataclass(frozen=True)
class ProjectMemoryLocation:
    """Resolved location of a project's memory file.

    Attributes:
        project_id: The stable hex identifier used as the directory name.
        identity: The string the id was derived from — either a git
            remote URL (preferred) or the canonical project path.
        directory: ``<profile_home>/projects/<project_id>/``
        memory_path: ``<directory>/memory.md``
        exists: True iff ``memory_path`` is a non-empty file at lookup time.
    """

    project_id: str
    identity: str
    directory: Path
    memory_path: Path
    exists: bool


def _git_remote_url(start: Path) -> str | None:
    """Return the first git ``origin`` URL discovered by walking up
    from ``start``. Returns ``None`` when ``start`` isn't inside a
    git repo, when git itself fails, or when no ``origin`` is set.

    Uses a 2s timeout because a wedged git process must not wedge
    callers.
    """
    if not start.exists():
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", str(start), "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        _LOG.debug("project_memory: git remote lookup failed for %s: %s", start, exc)
        return None
    if proc.returncode != 0:
        return None
    url = (proc.stdout or "").strip()
    return url or None


def _canonical_project_path(start: Path) -> Path:
    """Best-effort canonical absolute path for the project root.

    Walks upward to the nearest ``.git/`` directory and returns that
    root when found. When NO git root is found within 32 levels, the
    canonical path is the ``start`` directory itself — NOT the
    filesystem root. This is important: without that anchor, two
    unrelated non-git directories both walking to ``/`` would hash to
    the same project id and share memory.

    Symlinks resolved so two different paths into the same repo
    produce the same id.
    """
    try:
        cursor = start.resolve(strict=False)
    except (OSError, RuntimeError):
        cursor = start
    if not cursor.is_dir():
        cursor = cursor.parent
    anchor = cursor  # remember the starting point as the fallback
    for _ in range(32):
        try:
            if (cursor / ".git").exists():
                return cursor
        except OSError:
            break
        parent = cursor.parent
        if parent == cursor:
            break
        cursor = parent
    # No git root discovered — use the starting directory so two
    # unrelated non-git workspaces don't collide on the filesystem
    # root.
    return anchor


def _identity_for_project(start: Path) -> str:
    """Return the string used to derive the stable project id.

    Preference order:

      1. Git remote ``origin`` URL when discoverable — same project
         across clones / forks resolves to the same id.
      2. Canonical resolved project root path — fallback for non-git
         workspaces and repos without an origin set.
    """
    remote = _git_remote_url(start)
    if remote:
        return f"git:{remote}"
    return f"path:{_canonical_project_path(start)}"


def _hash_to_id(identity: str) -> str:
    """SHA-256(identity) → leading hex prefix. Deterministic across
    runs."""
    digest = hashlib.sha256(identity.encode("utf-8", errors="replace")).hexdigest()
    return digest[:_PROJECT_ID_HEX_LEN]


def project_id_for(cwd: Path | None = None) -> str:
    """Public helper: derive the stable project id for ``cwd`` (or
    the live ``os.getcwd()`` when ``cwd is None``).

    Always returns a hex string of length :data:`_PROJECT_ID_HEX_LEN`.
    Safe to call when ``cwd`` doesn't exist — falls back to ``"unknown"``
    string which still hashes to a stable id.
    """
    if cwd is None:
        try:
            cwd = Path(os.getcwd())
        except (FileNotFoundError, OSError):
            return _hash_to_id("path:<unknown>")
    return _hash_to_id(_identity_for_project(cwd))


def locate(
    cwd: Path | None = None,
    profile_home: Path | None = None,
) -> ProjectMemoryLocation:
    """Resolve the project-memory location without creating any files.

    Args:
        cwd: Directory whose project identity drives the lookup.
            Defaults to ``Path.cwd()``.
        profile_home: OC profile home. ``None`` means
            ``~/.opencomputer/`` (the default profile root). Caller
            passes an explicit profile_home in multi-profile setups.

    Returns:
        :class:`ProjectMemoryLocation`. The directory + file are NOT
        created here; ``exists`` reflects on-disk state at the
        moment of lookup.
    """
    if cwd is None:
        try:
            cwd = Path(os.getcwd())
        except (FileNotFoundError, OSError):
            cwd = Path("/")
    identity = _identity_for_project(cwd)
    pid = _hash_to_id(identity)
    if profile_home is None:
        try:
            profile_home = Path.home() / ".opencomputer"
        except (OSError, RuntimeError):
            profile_home = Path("/tmp")
    directory = profile_home / "projects" / pid
    memory_path = directory / "memory.md"
    exists = False
    try:
        exists = memory_path.is_file() and memory_path.stat().st_size > 0
    except OSError:
        exists = False
    return ProjectMemoryLocation(
        project_id=pid,
        identity=identity,
        directory=directory,
        memory_path=memory_path,
        exists=exists,
    )


def read(
    cwd: Path | None = None,
    profile_home: Path | None = None,
) -> str:
    """Return the project-memory body. Empty string when the file
    doesn't exist or can't be read. Never raises.
    """
    loc = locate(cwd=cwd, profile_home=profile_home)
    if not loc.exists:
        return ""
    try:
        return loc.memory_path.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeDecodeError) as exc:
        _LOG.warning(
            "project_memory.read: failed to read %s: %s", loc.memory_path, exc
        )
        return ""


def append(
    text: str,
    cwd: Path | None = None,
    profile_home: Path | None = None,
    *,
    timestamp: bool = True,
) -> bool:
    """Append a learning to the project's memory file.

    Args:
        text: The text to append. Whitespace-stripped; empty input is
            a no-op (returns False).
        cwd: Directory whose project identity drives the lookup.
        profile_home: OC profile home; ``None`` → default.
        timestamp: When True (default), prefix the entry with a
            short ISO-8601 UTC date marker (``## 2026-05-11T...``).
            Helpful for skimming the file later.

    Returns:
        True on success; False on no-op (empty input) or on
        unrecoverable write error. The error path logs at WARNING
        and never raises — callers can keep going if the auto-memory
        write fails.

    Side effects:
        Creates ``<profile_home>/projects/<id>/`` on demand. The
        directory tree is ``mkdir(parents=True, exist_ok=True)`` so
        concurrent creators don't race.
    """
    body = (text or "").strip()
    if not body:
        return False
    loc = locate(cwd=cwd, profile_home=profile_home)
    try:
        loc.directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _LOG.warning(
            "project_memory.append: cannot create %s: %s",
            loc.directory,
            exc,
        )
        return False
    if timestamp:
        ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        body = f"## {ts}\n{body}\n"
    else:
        body = f"{body}\n"
    try:
        # 'a' mode = O_APPEND on POSIX; atomic per-write.
        with loc.memory_path.open("a", encoding="utf-8") as f:
            if loc.memory_path.stat().st_size > 0:
                f.write("\n")
            f.write(body)
    except OSError as exc:
        _LOG.warning(
            "project_memory.append: failed to write %s: %s",
            loc.memory_path,
            exc,
        )
        return False
    return True


def clear(
    cwd: Path | None = None,
    profile_home: Path | None = None,
) -> bool:
    """Remove the project-memory file. Returns True if a file was
    removed; False if there was nothing to remove or the unlink
    failed. Used by ``oc memory prune --project``."""
    loc = locate(cwd=cwd, profile_home=profile_home)
    if not loc.memory_path.exists():
        return False
    try:
        loc.memory_path.unlink()
    except OSError as exc:
        _LOG.warning(
            "project_memory.clear: failed to unlink %s: %s",
            loc.memory_path,
            exc,
        )
        return False
    return True


__all__ = [
    "ProjectMemoryLocation",
    "append",
    "clear",
    "locate",
    "project_id_for",
    "read",
]

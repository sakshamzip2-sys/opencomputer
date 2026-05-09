"""Delegate isolation — per-subagent filesystem sandbox.

v1.1 plan-2 M4.1 + M4.2 (2026-05-09). Adds two opt-in modes for
``delegate(isolation=...)``:

* ``"worktree"`` — git worktree under
  ``<repo_root>/.opencomputer-worktrees/delegate-<uuid>/`` on a fresh
  branch ``oc-delegate-<uuid>``. Uses
  :func:`opencomputer.worktree.create_session_worktree` so it shares
  the orphan-recovery surface (``oc worktrees clean``).
* ``"copy"`` — a ``shutil.copytree`` of the parent's cwd into a
  ``tempfile.TemporaryDirectory``. Works on non-git cwd; honors
  ``.opencomputer/sandbox.ignore`` (gitignore-style) for skipping
  heavy directories. Cleaned up unconditionally on context exit.
* ``"none"`` (default) — no isolation; child runs in the parent's cwd.
  Equivalent to the pre-M4 behavior.

Cleanup posture:

* Worktree mode: if ``git status --porcelain`` in the worktree is
  empty after the child finishes, the worktree is removed. If it has
  uncommitted changes, the worktree PERSISTS (the operator can
  inspect, then ``oc worktrees clean`` later).
* Copy mode: always removed (no commit story; nothing to preserve).
* Crash safety: an ``atexit`` handler is registered for each isolation
  context so a parent-process exit (clean OR via SIGTERM) still tries
  to clean up. SIGKILL bypasses atexit — operator runs
  ``oc worktrees clean`` for those.

The IsolationContext is an ``async`` context manager so the delegate
flow can ``async with`` it without spinning up a thread for cleanup.
"""

from __future__ import annotations

import atexit
import logging
import shutil
import subprocess
import tempfile
import threading
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from opencomputer.worktree import (
    create_session_worktree,
    remove_session_worktree,
    repo_root,
)

logger = logging.getLogger("opencomputer.agent.delegate_isolation")

#: ``isolation=...`` accepted values.
IsolationMode = Literal["none", "worktree", "copy"]

#: Default name of the gitignore-style file that lists paths to skip
#: during ``copy`` mode. Looked up at the parent's cwd.
SANDBOX_IGNORE_FILE = ".opencomputer/sandbox.ignore"


class WorktreeNotAvailable(RuntimeError):  # noqa: N818
    """Raised when ``isolation="worktree"`` is requested outside a git repo.

    Name matches the plan's spec verbatim so callers that grep for
    `WorktreeNotAvailable` find the actual class.
    """


class IsolationFailed(RuntimeError):  # noqa: N818
    """Raised when an isolation sandbox could not be created.

    Distinct from :class:`WorktreeNotAvailable` so callers can branch
    on "wrong tool for the cwd" vs "right tool, infra failed".
    """


# Tracking for atexit-driven cleanup. We keep the live set of paths
# rather than registering one atexit per context (atexit fires in LIFO
# order; a single sweep is simpler and atomic).
_CLEANUP_LOCK = threading.Lock()
_PENDING_CLEANUP_WORKTREES: set[Path] = set()
_PENDING_CLEANUP_COPIES: set[Path] = set()
_ATEXIT_REGISTERED = False


def _atexit_sweep() -> None:
    """Best-effort cleanup of any isolation sandboxes still pending at exit."""
    with _CLEANUP_LOCK:
        worktrees = list(_PENDING_CLEANUP_WORKTREES)
        copies = list(_PENDING_CLEANUP_COPIES)
        _PENDING_CLEANUP_WORKTREES.clear()
        _PENDING_CLEANUP_COPIES.clear()
    for path in worktrees:
        try:
            if _is_clean_worktree(path):
                remove_session_worktree(path, force=True)
        except Exception:  # noqa: BLE001 — atexit must never raise
            pass
    for path in copies:
        try:
            shutil.rmtree(path, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass


def _ensure_atexit_registered() -> None:
    global _ATEXIT_REGISTERED
    with _CLEANUP_LOCK:
        if _ATEXIT_REGISTERED:
            return
        atexit.register(_atexit_sweep)
        _ATEXIT_REGISTERED = True


def _is_clean_worktree(wt_path: Path) -> bool:
    """Return True if ``git status --porcelain`` in ``wt_path`` is empty."""
    if not wt_path.exists():
        return True
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(wt_path),
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("worktree status check failed for %s: %s", wt_path, exc)
        return False
    return not result.stdout.strip()


@dataclass
class IsolationContext:
    """Result of :func:`acquire_isolation`. Carries cwd + cleanup metadata.

    ``mode`` is the literal isolation mode that won. ``cwd`` is the
    path the child agent should chdir into. ``persisted`` flips to
    True when worktree cleanup deferred because of uncommitted changes.
    """

    mode: IsolationMode
    cwd: Path
    persisted: bool = False
    #: Internal — paths the cleanup helper needs to remove. Empty for
    #: ``mode="none"``. Tests poke this directly to assert state.
    _cleanup_paths: list[Path] = field(default_factory=list)


def _read_sandbox_ignore(parent_cwd: Path) -> set[str]:
    """Parse ``.opencomputer/sandbox.ignore`` into a set of dir/file names.

    Empty/missing file → empty set (no skipping). One pattern per line;
    blank lines and lines starting with ``#`` are ignored. Patterns are
    matched against the BASENAME of each candidate (no glob support
    yet — keeps the contract small for v1).
    """
    ignore_path = parent_cwd / SANDBOX_IGNORE_FILE
    if not ignore_path.exists():
        return set()
    try:
        text = ignore_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("could not read %s: %s", ignore_path, exc)
        return set()
    out: set[str] = set()
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        out.add(s)
    return out


def _copy_with_ignore(src: Path, dst: Path, ignore_names: set[str]) -> None:
    """Copy ``src`` → ``dst``, skipping any direct child whose name is in ``ignore_names``."""

    def _ignore(_dirpath: str, names: list[str]) -> list[str]:
        # Drop any name in the ignore set, regardless of depth — keeps
        # node_modules/ etc. out at every level.
        return [n for n in names if n in ignore_names]

    shutil.copytree(src, dst, ignore=_ignore, symlinks=False, dirs_exist_ok=False)


@asynccontextmanager
async def acquire_isolation(
    mode: IsolationMode,
    *,
    parent_cwd: Path | None = None,
    delegate_id: str | None = None,
):
    """Async context manager that yields an :class:`IsolationContext`.

    ``mode="none"`` is a no-op (yields a context with ``cwd=parent_cwd``
    and no cleanup). The two non-trivial modes go through the existing
    worktree primitives or :func:`shutil.copytree`.

    Cleanup runs in the ``finally`` block so a child crash still
    triggers it. atexit registration is the secondary safety net for
    parent-process crashes that don't unwind context managers.
    """
    parent_cwd = parent_cwd or Path.cwd()
    delegate_id = delegate_id or uuid.uuid4().hex[:8]
    ctx = IsolationContext(mode=mode, cwd=parent_cwd)

    if mode == "none":
        yield ctx
        return

    if mode == "worktree":
        if repo_root(parent_cwd) is None:
            raise WorktreeNotAvailable(
                f"isolation='worktree' requested but {parent_cwd} is not "
                f"inside a git repo. Use isolation='copy' instead, or "
                f"omit isolation to share the parent's cwd."
            )
        wt_path = create_session_worktree(
            parent_cwd,
            session_id=f"delegate-{delegate_id}",
            branch=f"oc-delegate-{delegate_id}",
        )
        if wt_path is None:
            raise IsolationFailed(
                f"git worktree creation failed for delegate-{delegate_id} "
                f"(see preceding logs)."
            )
        ctx.cwd = wt_path
        ctx._cleanup_paths.append(wt_path)
        with _CLEANUP_LOCK:
            _PENDING_CLEANUP_WORKTREES.add(wt_path)
        _ensure_atexit_registered()
        try:
            yield ctx
            # Post-success cleanup: only remove if clean.
            if _is_clean_worktree(wt_path):
                remove_session_worktree(wt_path, force=True)
                with _CLEANUP_LOCK:
                    _PENDING_CLEANUP_WORKTREES.discard(wt_path)
            else:
                ctx.persisted = True
                logger.info(
                    "worktree %s has uncommitted changes; persisting for "
                    "operator review (run `oc worktrees clean` to remove)",
                    wt_path,
                )
                # Persist → drop from atexit sweep (operator owns it now)
                with _CLEANUP_LOCK:
                    _PENDING_CLEANUP_WORKTREES.discard(wt_path)
        except BaseException:
            # On failure: still try to clean — child may have written
            # nothing useful. _is_clean_worktree handles the dirty case.
            try:
                if _is_clean_worktree(wt_path):
                    remove_session_worktree(wt_path, force=True)
                    with _CLEANUP_LOCK:
                        _PENDING_CLEANUP_WORKTREES.discard(wt_path)
                else:
                    ctx.persisted = True
                    with _CLEANUP_LOCK:
                        _PENDING_CLEANUP_WORKTREES.discard(wt_path)
            except Exception:  # noqa: BLE001
                pass
            raise
        return

    if mode == "copy":
        ignore_names = _read_sandbox_ignore(parent_cwd)
        # Default ignores reduce the worst-case copy time to seconds even
        # on a large project.
        for default in (".git", "node_modules", ".venv", "target", "build", "dist"):
            ignore_names.add(default)
        # tempfile.mkdtemp guarantees uniqueness + 0o700 perms by default
        sandbox_root = Path(tempfile.mkdtemp(prefix=f"oc-delegate-{delegate_id}-"))
        try:
            _copy_with_ignore(parent_cwd, sandbox_root / "workspace", ignore_names)
        except OSError as exc:
            shutil.rmtree(sandbox_root, ignore_errors=True)
            raise IsolationFailed(
                f"copy sandbox creation failed: {exc}"
            ) from exc
        ctx.cwd = sandbox_root / "workspace"
        ctx._cleanup_paths.append(sandbox_root)
        with _CLEANUP_LOCK:
            _PENDING_CLEANUP_COPIES.add(sandbox_root)
        _ensure_atexit_registered()
        try:
            yield ctx
        finally:
            try:
                shutil.rmtree(sandbox_root, ignore_errors=True)
            except Exception:  # noqa: BLE001
                pass
            with _CLEANUP_LOCK:
                _PENDING_CLEANUP_COPIES.discard(sandbox_root)
        return

    # Defensive — Literal["none","worktree","copy"] makes this unreachable
    raise ValueError(f"unknown isolation mode: {mode!r}")


__all__ = [
    "SANDBOX_IGNORE_FILE",
    "IsolationContext",
    "IsolationFailed",
    "IsolationMode",
    "WorktreeNotAvailable",
    "acquire_isolation",
]

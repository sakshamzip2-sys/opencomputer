"""Worktree-per-session helper (Hermes Tier 2.B port).

When the user runs ``oc code --worktree`` (alias: ``-w``), spawn a fresh
git worktree under ``<repo>/.opencomputer-worktrees/<id>/`` and chdir
into it before the agent starts. On session exit (clean or via signal),
the worktree is removed.

Why a worktree per session?
- Parallel coding sessions don't step on each other's branches/state.
- Easy to discard experimental work — just exit, the dir vanishes.
- The repo's main worktree stays clean; the agent operates in isolation.

If the cwd isn't a git repo (or git isn't on PATH), the helper logs a
warning and returns the cwd unchanged. The flag never crashes the chat
loop — it's strictly additive.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

logger = logging.getLogger("opencomputer.worktree")

WORKTREES_DIR = ".opencomputer-worktrees"


def is_git_repo(path: Path) -> bool:
    """True if ``path`` is inside a git repo (has a reachable ``.git``)."""
    if not shutil.which("git"):
        return False
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(path),
            capture_output=True,
            text=True,
            timeout=5,
        )
        return out.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def repo_root(path: Path) -> Path | None:
    """Return the repo root for ``path``, or None if not inside a repo."""
    if not shutil.which("git"):
        return None
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(path),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode != 0:
            return None
        return Path(out.stdout.strip())
    except (subprocess.TimeoutExpired, OSError):
        return None


def create_session_worktree(
    cwd: Path,
    *,
    session_id: str | None = None,
    branch: str | None = None,
) -> Path | None:
    """Create a new worktree under ``<repo_root>/<WORKTREES_DIR>/<id>/``.

    Args:
        cwd: starting directory (need not be the repo root).
        session_id: optional id for the worktree dir name; default uses
            a short UUID slice.
        branch: optional branch name to create. Default: a fresh branch
            ``oc-session-<id>`` based on current HEAD.

    Returns the worktree path on success; ``None`` if the cwd isn't a
    repo or git invocation fails. Never raises — failures fall back to
    the original cwd.
    """
    root = repo_root(cwd)
    if root is None:
        logger.info("worktree: %s is not inside a git repo; skipping", cwd)
        return None

    sid = session_id or uuid.uuid4().hex[:8]
    worktrees_root = root / WORKTREES_DIR
    worktrees_root.mkdir(parents=True, exist_ok=True)
    wt_path = worktrees_root / sid

    if wt_path.exists():
        logger.warning("worktree path already exists: %s — using as-is", wt_path)
        return wt_path

    branch_name = branch or f"oc-session-{sid}"

    try:
        subprocess.run(
            ["git", "worktree", "add", str(wt_path), "-b", branch_name],
            cwd=str(root),
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        stderr = getattr(exc, "stderr", "") or ""
        logger.error("git worktree add failed: %s", stderr.strip())
        return None

    logger.info("created worktree %s on branch %s", wt_path, branch_name)
    return wt_path


def remove_session_worktree(wt_path: Path, *, force: bool = True) -> bool:
    """Remove a worktree previously created by :func:`create_session_worktree`.

    Args:
        wt_path: path returned by ``create_session_worktree``.
        force: pass ``--force`` to ``git worktree remove`` (default True
            so dirty worktrees still get cleaned up — by design, the
            session's edits were experimental).

    Returns True on success, False if the call failed (caller can choose
    to ignore — the caller process is exiting anyway). Never raises.
    """
    if not wt_path.exists():
        return True
    args = ["git", "worktree", "remove", str(wt_path)]
    if force:
        args.append("--force")
    try:
        subprocess.run(
            args,
            cwd=str(wt_path.parent),
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        stderr = getattr(exc, "stderr", "") or ""
        logger.warning("git worktree remove failed: %s — falling back to rmtree", stderr.strip())
        try:
            shutil.rmtree(wt_path, ignore_errors=True)
            return True
        except OSError as e2:
            logger.error("rmtree fallback also failed: %s", e2)
            return False


@contextmanager
def session_worktree(
    cwd: Path,
    *,
    session_id: str | None = None,
    branch: str | None = None,
    keep: bool = False,
) -> Iterator[Path]:
    """Context manager: create a worktree, chdir into it, clean up on exit.

    Yields the worktree path. If creation fails (not a repo / git missing),
    yields the original cwd unchanged.

    Args:
        cwd: starting directory.
        session_id: id for the worktree dir name.
        branch: optional branch to create.
        keep: if True, do NOT remove the worktree on exit. Useful when
            the user wants to inspect/keep the experimental branch.

    The chdir happens inside the contextmanager so the caller doesn't
    need to manage cwd state.
    """
    original_cwd = Path.cwd()
    wt = create_session_worktree(cwd, session_id=session_id, branch=branch)
    if wt is None:
        yield original_cwd
        return

    os.chdir(wt)
    try:
        yield wt
    finally:
        os.chdir(original_cwd)
        if not keep:
            remove_session_worktree(wt)


__all__ = [
    "WORKTREES_DIR",
    "create_session_worktree",
    "is_git_repo",
    "remove_session_worktree",
    "repo_root",
    "session_worktree",
]

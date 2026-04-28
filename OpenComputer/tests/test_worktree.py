"""Tests for opencomputer.worktree (Hermes Tier 2.B).

Each test creates a real git repo in tmp_path so the helper actually
runs `git worktree` against it. Skipped if `git` is not on PATH.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from opencomputer.worktree import (
    WORKTREES_DIR,
    create_session_worktree,
    is_git_repo,
    remove_session_worktree,
    repo_root,
    session_worktree,
)

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git not on PATH"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Initialize a real git repo in tmp_path with one commit."""
    subprocess.run(
        ["git", "init", "-q"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )
    # Configure user so commits work in CI / sandboxed envs.
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )
    (tmp_path / "README.md").write_text("hello\n")
    subprocess.run(
        ["git", "add", "."],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "init"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )
    return tmp_path


# ---------------------------------------------------------------------------
# is_git_repo / repo_root
# ---------------------------------------------------------------------------


def test_is_git_repo_true(repo: Path):
    assert is_git_repo(repo)


def test_is_git_repo_false(tmp_path: Path):
    bare = tmp_path / "not-a-repo"
    bare.mkdir()
    assert not is_git_repo(bare)


def test_repo_root(repo: Path):
    sub = repo / "sub"
    sub.mkdir()
    root = repo_root(sub)
    assert root is not None
    # Resolved paths may differ via symlinks (e.g. /tmp vs /private/tmp on macOS);
    # comparison via realpath handles that.
    assert os.path.realpath(root) == os.path.realpath(repo)


def test_repo_root_outside(tmp_path: Path):
    bare = tmp_path / "not-a-repo"
    bare.mkdir()
    assert repo_root(bare) is None


# ---------------------------------------------------------------------------
# create_session_worktree
# ---------------------------------------------------------------------------


def test_create_basic(repo: Path):
    wt = create_session_worktree(repo, session_id="abc")
    assert wt is not None
    assert wt.exists()
    assert wt.parent.name == WORKTREES_DIR
    assert wt.name == "abc"
    # Worktree has README.md from main commit
    assert (wt / "README.md").exists()


def test_create_outside_repo_returns_none(tmp_path: Path):
    bare = tmp_path / "not-a-repo"
    bare.mkdir()
    wt = create_session_worktree(bare)
    assert wt is None


def test_create_uses_random_id_when_unspecified(repo: Path):
    wt = create_session_worktree(repo)
    assert wt is not None
    # Default id is uuid hex slice (8 chars).
    assert len(wt.name) == 8


def test_create_with_custom_branch(repo: Path):
    wt = create_session_worktree(repo, session_id="t", branch="my-branch")
    assert wt is not None
    out = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=str(wt),
        capture_output=True,
        text=True,
    )
    assert out.stdout.strip() == "my-branch"


# ---------------------------------------------------------------------------
# remove_session_worktree
# ---------------------------------------------------------------------------


def test_remove_basic(repo: Path):
    wt = create_session_worktree(repo, session_id="rm-me")
    assert wt is not None
    ok = remove_session_worktree(wt)
    assert ok
    assert not wt.exists()


def test_remove_dirty_worktree_with_force(repo: Path):
    wt = create_session_worktree(repo, session_id="dirty")
    assert wt is not None
    (wt / "uncommitted.txt").write_text("dirty")
    ok = remove_session_worktree(wt, force=True)
    assert ok
    assert not wt.exists()


def test_remove_nonexistent_is_noop(tmp_path: Path):
    """Removing a non-existent worktree returns True (idempotent)."""
    ghost = tmp_path / "ghost"
    assert remove_session_worktree(ghost) is True


# ---------------------------------------------------------------------------
# session_worktree context manager
# ---------------------------------------------------------------------------


def test_session_worktree_chdirs_in_and_out(repo: Path):
    original = Path.cwd()
    with session_worktree(repo, session_id="ctx") as wt:
        assert os.path.realpath(Path.cwd()) == os.path.realpath(wt)
        assert wt != original
    assert os.path.realpath(Path.cwd()) == os.path.realpath(original)


def test_session_worktree_cleans_up_on_exit(repo: Path):
    with session_worktree(repo, session_id="cleanup") as wt:
        assert wt.exists()
    # On normal exit, the worktree should be removed.
    assert not (repo / WORKTREES_DIR / "cleanup").exists()


def test_session_worktree_keep_preserves(repo: Path):
    with session_worktree(repo, session_id="keep", keep=True) as wt:
        assert wt.exists()
    assert (repo / WORKTREES_DIR / "keep").exists()


def test_session_worktree_outside_repo_yields_cwd(tmp_path: Path):
    bare = tmp_path / "not-a-repo"
    bare.mkdir()
    original = Path.cwd()
    with session_worktree(bare) as wt:
        # Not a repo → yields the original cwd unchanged.
        assert wt == original
    # cwd should also be unchanged.
    assert Path.cwd() == original


def test_session_worktree_cleans_up_on_exception(repo: Path):
    """Even when the body raises, cleanup runs."""
    try:
        with session_worktree(repo, session_id="raise"):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert not (repo / WORKTREES_DIR / "raise").exists()

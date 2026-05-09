"""M4.1 + M4.2 — delegate isolation (worktree + copy).

Pins the contract added 2026-05-09:

* :func:`acquire_isolation` async context manager (3 modes).
* :class:`WorktreeNotAvailable` raised on non-git cwd with mode='worktree'.
* Cleanup posture: worktree cleaned only when ``git status`` clean;
  copy cleaned unconditionally; atexit handles parent-crash leftovers.
* Sandbox-ignore file (``.opencomputer/sandbox.ignore``) honored in copy mode.

These tests exercise the isolation primitives directly. Integration
with :class:`DelegateTool` is verified at the schema level (the
``isolation`` parameter is enumerated in the tool schema) — full
end-to-end delegate testing belongs to the existing delegate test
suite.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from opencomputer.agent.delegate_isolation import (
    SANDBOX_IGNORE_FILE,
    IsolationContext,
    IsolationFailed,
    WorktreeNotAvailable,
    acquire_isolation,
)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ─── mode='none' ─────────────────────────────────────────────────────────


class TestNoneMode:
    def test_none_mode_yields_parent_cwd(self, tmp_path: Path) -> None:
        async def _go() -> IsolationContext:
            async with acquire_isolation("none", parent_cwd=tmp_path) as ctx:
                return ctx

        ctx = _run(_go())
        assert ctx.mode == "none"
        assert ctx.cwd == tmp_path
        assert ctx.persisted is False


# ─── mode='worktree' ─────────────────────────────────────────────────────


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "test"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    (path / "README.md").write_text("seed")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=path,
        check=True,
        capture_output=True,
    )


class TestWorktreeMode:
    def test_worktree_mode_outside_git_raises(self, tmp_path: Path) -> None:
        async def _go() -> None:
            async with acquire_isolation("worktree", parent_cwd=tmp_path) as _:
                pass

        with pytest.raises(WorktreeNotAvailable):
            _run(_go())

    def test_worktree_mode_creates_and_cleans_clean_worktree(
        self, tmp_path: Path
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_git_repo(repo)

        wt_path_holder: dict[str, Path] = {}

        async def _go() -> IsolationContext:
            async with acquire_isolation(
                "worktree", parent_cwd=repo, delegate_id="abc12345"
            ) as ctx:
                wt_path_holder["path"] = ctx.cwd
                # Verify the worktree exists during the with-block
                assert ctx.cwd.exists()
                assert ctx.cwd != repo
                # Don't make any modifications — `git status` will be clean
                return ctx

        ctx = _run(_go())
        assert ctx.mode == "worktree"
        assert ctx.persisted is False
        # Post-exit: worktree should be removed because clean
        assert not wt_path_holder["path"].exists()

    def test_worktree_mode_persists_dirty_worktree(
        self, tmp_path: Path
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_git_repo(repo)

        wt_path_holder: dict[str, Path] = {}

        async def _go() -> IsolationContext:
            async with acquire_isolation(
                "worktree", parent_cwd=repo, delegate_id="dirty01"
            ) as ctx:
                # Make a change so git status reports dirty
                (ctx.cwd / "new_file.txt").write_text("hello")
                wt_path_holder["path"] = ctx.cwd
                return ctx

        ctx = _run(_go())
        assert ctx.mode == "worktree"
        assert ctx.persisted is True
        # Post-exit: dirty worktree persists for operator review
        assert wt_path_holder["path"].exists()
        # Cleanup so the test doesn't pollute the tmpdir
        from opencomputer.worktree import remove_session_worktree

        remove_session_worktree(wt_path_holder["path"], force=True)


# ─── mode='copy' ─────────────────────────────────────────────────────────


class TestCopyMode:
    def test_copy_mode_creates_sandbox(self, tmp_path: Path) -> None:
        # Seed parent cwd with a file
        (tmp_path / "important.txt").write_text("data")
        sandbox_holder: dict[str, Path] = {}

        async def _go() -> IsolationContext:
            async with acquire_isolation(
                "copy", parent_cwd=tmp_path, delegate_id="copy123"
            ) as ctx:
                assert ctx.cwd.exists()
                assert ctx.cwd != tmp_path
                # File should have been copied
                assert (ctx.cwd / "important.txt").read_text() == "data"
                sandbox_holder["path"] = ctx.cwd.parent  # the tempdir root
                return ctx

        ctx = _run(_go())
        assert ctx.mode == "copy"
        # Post-exit: sandbox always cleaned
        assert not sandbox_holder["path"].exists()

    def test_copy_mode_works_on_non_git_cwd(self, tmp_path: Path) -> None:
        # No git init — should still work in copy mode
        async def _go() -> IsolationContext:
            async with acquire_isolation("copy", parent_cwd=tmp_path) as ctx:
                return ctx

        ctx = _run(_go())
        assert ctx.mode == "copy"

    def test_copy_mode_honors_sandbox_ignore(self, tmp_path: Path) -> None:
        # Seed with both a normal dir and a node_modules-like dir
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("print('hi')")
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / "huge.bin").write_text("X" * 1000)

        # Default ignore set excludes node_modules without the user file
        async def _go() -> Path:
            async with acquire_isolation("copy", parent_cwd=tmp_path) as ctx:
                return ctx.cwd

        sandbox_cwd = _run(_go())
        # During the with-block, node_modules was excluded by default;
        # the sandbox is now cleaned, so we test by inspecting the
        # call before cleanup. Use a different style:
        sandbox_cwd_holder: dict[str, Path] = {}

        async def _go2() -> None:
            async with acquire_isolation("copy", parent_cwd=tmp_path) as ctx:
                sandbox_cwd_holder["cwd"] = ctx.cwd
                # Inspect inside the with-block before cleanup
                assert (ctx.cwd / "src" / "main.py").exists()
                assert not (ctx.cwd / "node_modules").exists()

        _run(_go2())

    def test_copy_mode_honors_user_sandbox_ignore_file(
        self, tmp_path: Path
    ) -> None:
        # Seed with a custom dir + the user ignore file
        (tmp_path / "secret_dir").mkdir()
        (tmp_path / "secret_dir" / "x.txt").write_text("private")
        (tmp_path / "ok_dir").mkdir()
        (tmp_path / "ok_dir" / "y.txt").write_text("public")
        (tmp_path / ".opencomputer").mkdir()
        (tmp_path / ".opencomputer" / "sandbox.ignore").write_text(
            "# Skip secrets\nsecret_dir\n"
        )

        async def _go() -> None:
            async with acquire_isolation("copy", parent_cwd=tmp_path) as ctx:
                assert (ctx.cwd / "ok_dir" / "y.txt").exists()
                assert not (ctx.cwd / "secret_dir").exists()

        _run(_go())


# ─── invalid mode ────────────────────────────────────────────────────────


class TestInvalidMode:
    def test_unknown_mode_raises_value_error(self, tmp_path: Path) -> None:
        async def _go() -> None:
            async with acquire_isolation("invalid", parent_cwd=tmp_path) as _:  # type: ignore[arg-type]
                pass

        with pytest.raises(ValueError, match="unknown isolation mode"):
            _run(_go())


# ─── delegate schema integration ─────────────────────────────────────────


class TestDelegateSchemaWiring:
    def test_delegate_schema_includes_isolation_param(self) -> None:
        from opencomputer.tools.delegate import DelegateTool

        schema = DelegateTool().schema
        params = schema.parameters
        assert "isolation" in params["properties"]
        iso_def = params["properties"]["isolation"]
        assert iso_def["type"] == "string"
        assert set(iso_def["enum"]) == {"none", "worktree", "copy"}


# ─── constants ───────────────────────────────────────────────────────────


class TestConstants:
    def test_sandbox_ignore_file_constant(self) -> None:
        assert SANDBOX_IGNORE_FILE == ".opencomputer/sandbox.ignore"

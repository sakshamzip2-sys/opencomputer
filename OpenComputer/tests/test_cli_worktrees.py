"""Tests for ``oc worktrees`` Typer subapp."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from opencomputer.cli_worktrees import worktrees_app

runner = CliRunner()


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(repo),
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "T",
            "GIT_AUTHOR_EMAIL": "t@e",
            "GIT_COMMITTER_NAME": "T",
            "GIT_COMMITTER_EMAIL": "t@e",
        },
    )


def test_worktrees_list_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "f").write_text("x")
    _git(repo, "add", "f")
    _git(repo, "commit", "-m", "i")

    monkeypatch.chdir(repo)
    result = runner.invoke(worktrees_app, ["list"])
    assert result.exit_code == 0
    assert "no oc worktrees" in result.output.lower()


def test_worktrees_list_populated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "f").write_text("x")
    _git(repo, "add", "f")
    _git(repo, "commit", "-m", "i")

    wts = repo / ".opencomputer-worktrees"
    wts.mkdir()
    sid = "abc123"
    _git(repo, "worktree", "add", str(wts / sid), "-b", f"oc-session-{sid}")

    monkeypatch.chdir(repo)
    result = runner.invoke(worktrees_app, ["list"])
    assert result.exit_code == 0
    assert sid in result.output


def test_worktrees_clean_dry_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "f").write_text("x")
    _git(repo, "add", "f")
    _git(repo, "commit", "-m", "i")

    wts = repo / ".opencomputer-worktrees"
    wts.mkdir()
    # Create a leftover dir not registered with git.
    (wts / "stale").mkdir()

    monkeypatch.chdir(repo)
    result = runner.invoke(worktrees_app, ["clean", "--dry-run"])
    assert result.exit_code == 0
    assert "stale" in result.output
    assert (wts / "stale").exists()  # dry-run preserved


def test_worktrees_clean_removes_stale(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "f").write_text("x")
    _git(repo, "add", "f")
    _git(repo, "commit", "-m", "i")

    wts = repo / ".opencomputer-worktrees"
    wts.mkdir()
    (wts / "stale").mkdir()  # leftover with no git registration

    monkeypatch.chdir(repo)
    result = runner.invoke(worktrees_app, ["clean"])
    assert result.exit_code == 0
    assert not (wts / "stale").exists()


def test_worktrees_include_preview_format(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "f").write_text("x")
    _git(repo, "add", "f")
    _git(repo, "commit", "-m", "i")
    (repo / ".env").write_text("X=1")
    (repo / ".worktreeinclude").write_text(".env\n")

    monkeypatch.chdir(repo)
    result = runner.invoke(worktrees_app, ["include-preview"])
    assert result.exit_code == 0
    assert ".env" in result.output

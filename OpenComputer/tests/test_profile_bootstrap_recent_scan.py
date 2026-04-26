"""Layered Awareness MVP — Layer 2 file + git scan tests."""
import time
from pathlib import Path

import pytest

from opencomputer.profile_bootstrap.recent_scan import (
    GitCommitSummary,
    RecentFileSummary,
    scan_git_log,
    scan_recent_files,
)


def test_scan_recent_files_returns_recent(tmp_path: Path):
    f = tmp_path / "doc.md"
    f.write_text("Hello world")
    found = scan_recent_files(roots=[tmp_path], days=7)
    assert len(found) == 1
    assert found[0].path == str(f.resolve())
    assert found[0].size_bytes > 0


def test_scan_recent_files_skips_old(tmp_path: Path):
    f = tmp_path / "old.md"
    f.write_text("old content")
    old_time = time.time() - 30 * 24 * 3600  # 30 days ago
    import os
    os.utime(f, (old_time, old_time))
    found = scan_recent_files(roots=[tmp_path], days=7)
    assert found == []


def test_scan_recent_files_skips_dotfiles(tmp_path: Path):
    f = tmp_path / ".env"
    f.write_text("SECRET=abc")
    found = scan_recent_files(roots=[tmp_path], days=7)
    assert found == []


def test_scan_recent_files_returns_empty_when_root_missing(tmp_path: Path):
    found = scan_recent_files(roots=[tmp_path / "nope"], days=7)
    assert found == []


def test_scan_git_log_returns_commits(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()  # marker only — we mock subprocess

    fake_log = (
        "abc123def456\t1714000000\tsaksham@example.com\tInitial commit\n"
        "def456ghi789\t1714086400\tsaksham@example.com\tSecond commit\n"
    )

    class _R:
        returncode = 0
        stdout = fake_log

    def fake_run(*args, **kwargs):
        return _R()

    monkeypatch.setattr(
        "opencomputer.profile_bootstrap.recent_scan.subprocess.run", fake_run
    )

    commits = scan_git_log(repo_paths=[repo], days=7)
    assert len(commits) == 2
    assert commits[0].sha == "abc123def456"
    assert commits[0].subject == "Initial commit"


def test_scan_git_log_skips_non_repo(tmp_path: Path):
    not_repo = tmp_path / "plain_dir"
    not_repo.mkdir()
    commits = scan_git_log(repo_paths=[not_repo], days=7)
    assert commits == []

"""Tests for opencomputer.worktree_include — Section A of the spec."""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

from opencomputer.worktree_include import (
    CopyEntry,
    CopyReport,
    WorktreeIncludeTooLargeError,
)

# ─── Type tests (T2) ────────────────────────────────────────────────


def test_copy_entry_basic() -> None:
    e = CopyEntry(src=Path("/a/b"), dst=Path("/c/d"), bytes_copied=42)
    assert e.src == Path("/a/b")
    assert e.bytes_copied == 42


def test_copy_report_defaults() -> None:
    r = CopyReport()
    assert r.copied == ()
    assert r.skipped == ()
    assert r.failed == ()
    assert r.total_bytes == 0
    assert r.dry_run is False


def test_copy_report_explicit_total() -> None:
    r = CopyReport(
        copied=(
            CopyEntry(src=Path("/x"), dst=Path("/y"), bytes_copied=10),
            CopyEntry(src=Path("/x2"), dst=Path("/y2"), bytes_copied=20),
        ),
        total_bytes=30,
    )
    assert r.total_bytes == 30


def test_too_large_error_carries_metadata() -> None:
    err = WorktreeIncludeTooLargeError(
        total_bytes=2_000_000_000,
        cap_bytes=1_000_000_000,
        oversize_paths=(Path("/big"),),
    )
    assert err.total_bytes == 2_000_000_000
    assert err.cap_bytes == 1_000_000_000
    assert err.oversize_paths == (Path("/big"),)
    msg = str(err)
    assert "2,000,000,000" in msg


# ─── parse_worktreeinclude (T3) ─────────────────────────────────────


from opencomputer.worktree_include import parse_worktreeinclude  # noqa: E402


def test_parse_worktreeinclude_basic(tmp_path: Path) -> None:
    f = tmp_path / ".worktreeinclude"
    f.write_text(".env\n.venv/\nconfig/*.local.yaml\n")
    assert parse_worktreeinclude(f) == [".env", ".venv/", "config/*.local.yaml"]


def test_parse_worktreeinclude_strips_comments_and_blanks(tmp_path: Path) -> None:
    f = tmp_path / ".worktreeinclude"
    f.write_text(
        "# comment\n"
        "\n"
        ".env       \n"
        "  # leading-space comment is also a comment\n"
        ".venv/\n"
        "\n"
    )
    assert parse_worktreeinclude(f) == [".env", ".venv/"]


def test_parse_worktreeinclude_missing_file_returns_empty(tmp_path: Path) -> None:
    assert parse_worktreeinclude(tmp_path / "nope") == []


def test_parse_worktreeinclude_invalid_utf8_returns_empty(tmp_path: Path) -> None:
    f = tmp_path / ".worktreeinclude"
    f.write_bytes(b"\xff\xfe\x00broken")
    assert parse_worktreeinclude(f) == []


# ─── expand_patterns (T4) ───────────────────────────────────────────


from opencomputer.worktree_include import expand_patterns  # noqa: E402


def test_expand_literal_file(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("X=1")
    matched = expand_patterns(tmp_path, [".env"])
    assert matched == [tmp_path / ".env"]


def test_expand_literal_directory(tmp_path: Path) -> None:
    d = tmp_path / ".venv"
    d.mkdir()
    (d / "marker").write_text("ok")
    assert expand_patterns(tmp_path, [".venv/"]) == [d]
    assert expand_patterns(tmp_path, [".venv"]) == [d]


def test_expand_glob(tmp_path: Path) -> None:
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "a.local.yaml").write_text("a")
    (cfg / "b.local.yaml").write_text("b")
    (cfg / "c.public.yaml").write_text("c")
    matched = expand_patterns(tmp_path, ["config/*.local.yaml"])
    assert sorted(matched) == sorted([cfg / "a.local.yaml", cfg / "b.local.yaml"])


def test_expand_no_match_returns_empty(tmp_path: Path) -> None:
    assert expand_patterns(tmp_path, ["nothing_here.txt"]) == []


def test_expand_dedupes_across_patterns(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("x")
    matched = expand_patterns(tmp_path, [".env", ".env"])
    assert matched == [tmp_path / ".env"]


def test_expand_rejects_escape_repo_root(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("o")
    matched = expand_patterns(repo, ["../outside"])
    assert matched == []


# ─── copy_into_worktree (T5) ────────────────────────────────────────


from opencomputer.worktree_include import copy_into_worktree  # noqa: E402


def test_copy_file_preserves_mode_mtime(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    wt = tmp_path / "wt"
    repo.mkdir()
    wt.mkdir()
    src = repo / ".env"
    src.write_text("API_KEY=abc")
    src.chmod(0o600)
    expected_mtime = src.stat().st_mtime

    report = copy_into_worktree([src], repo, wt)
    dst = wt / ".env"
    assert dst.exists()
    assert dst.read_text() == "API_KEY=abc"
    assert stat.S_IMODE(dst.stat().st_mode) == 0o600
    assert dst.stat().st_mtime == pytest.approx(expected_mtime, abs=1)
    assert len(report.copied) == 1
    assert report.copied[0].src == src
    assert report.copied[0].dst == dst
    assert report.total_bytes == len("API_KEY=abc")
    assert report.dry_run is False


def test_copy_directory_recursive(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    wt = tmp_path / "wt"
    repo.mkdir()
    wt.mkdir()
    venv = repo / ".venv"
    venv.mkdir()
    (venv / "pyvenv.cfg").write_text("home = /usr")
    (venv / "bin").mkdir()
    (venv / "bin" / "python").write_text("#!/usr/bin/env python3")

    report = copy_into_worktree([venv], repo, wt)
    assert (wt / ".venv" / "pyvenv.cfg").read_text() == "home = /usr"
    assert (wt / ".venv" / "bin" / "python").read_text() == "#!/usr/bin/env python3"
    assert len(report.copied) == 2


def test_copy_atomic_temp_rename_no_leftover(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    wt = tmp_path / "wt"
    repo.mkdir()
    wt.mkdir()
    (repo / "a").write_text("data")
    copy_into_worktree([repo / "a"], repo, wt)
    leftovers = [
        p for p in wt.iterdir() if p.name.startswith(".") and ".tmp." in p.name
    ]
    assert leftovers == []


def test_copy_dry_run_no_io(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    wt = tmp_path / "wt"
    repo.mkdir()
    wt.mkdir()
    (repo / "a").write_text("xy")

    report = copy_into_worktree([repo / "a"], repo, wt, dry_run=True)
    assert report.dry_run is True
    assert len(report.copied) == 1
    assert report.copied[0].bytes_copied == 2
    assert not (wt / "a").exists()


def test_copy_size_cap_aborts(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    wt = tmp_path / "wt"
    repo.mkdir()
    wt.mkdir()
    big = repo / "big"
    big.write_bytes(b"x" * 100)

    with pytest.raises(WorktreeIncludeTooLargeError) as exc_info:
        copy_into_worktree([big], repo, wt, max_total_mb=0)
    assert exc_info.value.total_bytes == 100
    assert not (wt / "big").exists()


def test_copy_per_file_size_skips(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    wt = tmp_path / "wt"
    repo.mkdir()
    wt.mkdir()
    small = repo / "small"
    small.write_bytes(b"x" * 10)
    big = repo / "big"
    big.write_bytes(b"x" * (2 * 1024 * 1024))

    report = copy_into_worktree([small, big], repo, wt, max_per_file_mb=1)
    assert (wt / "small").exists()
    assert not (wt / "big").exists()
    assert any(p == big for p, _ in report.skipped)


# ─── symlink + cycle handling (T6) ──────────────────────────────────


def test_copy_symlink_no_follow(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    wt = tmp_path / "wt"
    repo.mkdir()
    wt.mkdir()
    real = repo / "real.txt"
    real.write_text("R")
    link = repo / "link.txt"
    link.symlink_to("real.txt")

    copy_into_worktree([link], repo, wt, follow_symlinks=False)
    dst = wt / "link.txt"
    assert dst.is_symlink()
    assert os.readlink(dst) == "real.txt"


def test_copy_recursive_symlink_cycle_skipped(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    wt = tmp_path / "wt"
    repo.mkdir()
    wt.mkdir()
    a = repo / "a"
    b = a / "b"
    a.mkdir()
    b.mkdir()
    # b/back -> a   (cycle)
    (b / "back").symlink_to(a, target_is_directory=True)

    report = copy_into_worktree([a], repo, wt)
    assert (wt / "a").exists()
    # No infinite recursion happened (test would hang).
    # The cycle handler may copy the symlink itself but not recurse into it.


# ─── apply_to_worktree integration (T7) ─────────────────────────────


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


def test_session_worktree_applies_include(tmp_path: Path) -> None:
    """End-to-end: real `git init` + worktree add + .worktreeinclude copies."""
    from opencomputer.worktree import session_worktree

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "README.md").write_text("# r")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")

    (repo / ".env").write_text("API=KEY")
    (repo / ".gitignore").write_text(".env\n")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-m", "ignore .env")

    (repo / ".worktreeinclude").write_text(".env\n")
    _git(repo, "add", ".worktreeinclude")
    _git(repo, "commit", "-m", "add include manifest")

    with session_worktree(repo, session_id="testwt") as wt:
        assert wt.is_dir()
        assert (wt / ".env").read_text() == "API=KEY"
        assert (wt / "README.md").exists()


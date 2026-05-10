"""CC §8 — per-project auto-memory module.

Spec: docs/OC-FROM-CLAUDE-CODE.md §8. Provides per-project memory
files at ``<profile_home>/projects/<id>/memory.md``. Standalone module;
no parallel-session collision risk on memory.py.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from opencomputer.agent.project_memory import (
    ProjectMemoryLocation,
    append,
    clear,
    locate,
    project_id_for,
    read,
)


def _init_git_repo(path: Path) -> None:
    """Initialise an empty git repo at ``path`` for tests that need
    one. Captures stdout so test output stays clean."""
    subprocess.run(
        ["git", "init", "-q", str(path)],
        check=True,
        capture_output=True,
    )


# ─── project_id_for ───────────────────────────────────────────────────


def test_project_id_is_stable_across_calls(tmp_path):
    a = project_id_for(tmp_path)
    b = project_id_for(tmp_path)
    assert a == b


def test_project_id_is_16_hex_chars(tmp_path):
    pid = project_id_for(tmp_path)
    assert re.fullmatch(r"[0-9a-f]{16}", pid), (
        f"project_id should be 16 lowercase hex chars; got {pid!r}"
    )


def test_different_paths_yield_different_ids(tmp_path):
    a = tmp_path / "proj-a"
    b = tmp_path / "proj-b"
    a.mkdir()
    b.mkdir()
    assert project_id_for(a) != project_id_for(b)


def test_project_id_handles_nonexistent_cwd():
    """A vanished cwd doesn't crash the helper."""
    pid = project_id_for(Path("/this/does/not/exist"))
    assert re.fullmatch(r"[0-9a-f]{16}", pid)


def test_project_id_uses_git_remote_when_present(tmp_path):
    """Two checkouts of the same repo (same remote) hash to the same id
    even though their on-disk paths differ."""
    co_a = tmp_path / "checkout-a"
    co_b = tmp_path / "checkout-b"
    co_a.mkdir()
    co_b.mkdir()
    _init_git_repo(co_a)
    _init_git_repo(co_b)
    for co in (co_a, co_b):
        subprocess.run(
            ["git", "-C", str(co), "remote", "add", "origin",
             "https://example.test/team/proj.git"],
            check=True,
            capture_output=True,
        )
    assert project_id_for(co_a) == project_id_for(co_b)


def test_project_id_different_remotes_different_ids(tmp_path):
    co_a = tmp_path / "a"
    co_b = tmp_path / "b"
    co_a.mkdir()
    co_b.mkdir()
    _init_git_repo(co_a)
    _init_git_repo(co_b)
    subprocess.run(
        ["git", "-C", str(co_a), "remote", "add", "origin", "https://x.test/A.git"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(co_b), "remote", "add", "origin", "https://x.test/B.git"],
        check=True,
        capture_output=True,
    )
    assert project_id_for(co_a) != project_id_for(co_b)


# ─── locate ──────────────────────────────────────────────────────────


def test_locate_returns_expected_paths(tmp_path):
    profile = tmp_path / "profile"
    proj = tmp_path / "myproj"
    proj.mkdir()
    loc = locate(cwd=proj, profile_home=profile)
    assert isinstance(loc, ProjectMemoryLocation)
    assert loc.directory == profile / "projects" / loc.project_id
    assert loc.memory_path == loc.directory / "memory.md"
    # exists=False because we haven't written anything yet.
    assert loc.exists is False


def test_locate_does_not_create_directory(tmp_path):
    """Mere lookup must not touch the filesystem."""
    profile = tmp_path / "profile"
    proj = tmp_path / "myproj"
    proj.mkdir()
    locate(cwd=proj, profile_home=profile)
    assert not (profile / "projects").exists()


def test_locate_reports_exists_after_write(tmp_path):
    profile = tmp_path / "profile"
    proj = tmp_path / "myproj"
    proj.mkdir()
    append("first learning", cwd=proj, profile_home=profile)
    loc = locate(cwd=proj, profile_home=profile)
    assert loc.exists is True


# ─── append + read ────────────────────────────────────────────────────


def test_append_creates_file_and_directory(tmp_path):
    profile = tmp_path / "profile"
    proj = tmp_path / "myproj"
    proj.mkdir()
    ok = append("learned X", cwd=proj, profile_home=profile)
    assert ok is True
    loc = locate(cwd=proj, profile_home=profile)
    assert loc.memory_path.exists()


def test_append_text_round_trips_via_read(tmp_path):
    profile = tmp_path / "profile"
    proj = tmp_path / "myproj"
    proj.mkdir()
    append("important fact", cwd=proj, profile_home=profile)
    body = read(cwd=proj, profile_home=profile)
    assert "important fact" in body


def test_read_returns_empty_for_unwritten_project(tmp_path):
    profile = tmp_path / "profile"
    proj = tmp_path / "myproj"
    proj.mkdir()
    body = read(cwd=proj, profile_home=profile)
    assert body == ""


def test_append_with_empty_string_is_noop(tmp_path):
    profile = tmp_path / "profile"
    proj = tmp_path / "myproj"
    proj.mkdir()
    ok = append("", cwd=proj, profile_home=profile)
    assert ok is False
    # File NOT created — empty input means "no learning to record."
    assert not (profile / "projects").exists() or not (
        locate(cwd=proj, profile_home=profile).memory_path.exists()
    )


def test_append_with_whitespace_only_is_noop(tmp_path):
    profile = tmp_path / "profile"
    proj = tmp_path / "myproj"
    proj.mkdir()
    ok = append("   \n  \t", cwd=proj, profile_home=profile)
    assert ok is False


def test_append_writes_timestamp_marker_by_default(tmp_path):
    profile = tmp_path / "profile"
    proj = tmp_path / "myproj"
    proj.mkdir()
    append("note A", cwd=proj, profile_home=profile)
    body = read(cwd=proj, profile_home=profile)
    # ISO-8601 marker contains a 'T' separator and 'Z' UTC indicator.
    assert re.search(r"##\s+\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", body)


def test_append_skips_timestamp_when_disabled(tmp_path):
    profile = tmp_path / "profile"
    proj = tmp_path / "myproj"
    proj.mkdir()
    append("plain", cwd=proj, profile_home=profile, timestamp=False)
    body = read(cwd=proj, profile_home=profile)
    assert not re.search(r"##\s+\d{4}-\d{2}-\d{2}T", body)
    assert "plain" in body


def test_append_multiple_entries_preserved(tmp_path):
    profile = tmp_path / "profile"
    proj = tmp_path / "myproj"
    proj.mkdir()
    append("first", cwd=proj, profile_home=profile)
    append("second", cwd=proj, profile_home=profile)
    append("third", cwd=proj, profile_home=profile)
    body = read(cwd=proj, profile_home=profile)
    assert "first" in body
    assert "second" in body
    assert "third" in body
    # Order preserved (append-only).
    assert body.find("first") < body.find("second") < body.find("third")


# ─── clear ───────────────────────────────────────────────────────────


def test_clear_removes_file(tmp_path):
    profile = tmp_path / "profile"
    proj = tmp_path / "myproj"
    proj.mkdir()
    append("about to delete", cwd=proj, profile_home=profile)
    assert read(cwd=proj, profile_home=profile)  # non-empty
    removed = clear(cwd=proj, profile_home=profile)
    assert removed is True
    assert read(cwd=proj, profile_home=profile) == ""


def test_clear_no_file_returns_false(tmp_path):
    profile = tmp_path / "profile"
    proj = tmp_path / "myproj"
    proj.mkdir()
    removed = clear(cwd=proj, profile_home=profile)
    assert removed is False


# ─── isolation ───────────────────────────────────────────────────────


def test_two_projects_have_independent_memories(tmp_path):
    profile = tmp_path / "profile"
    proj_a = tmp_path / "a"
    proj_b = tmp_path / "b"
    proj_a.mkdir()
    proj_b.mkdir()
    append("A says hi", cwd=proj_a, profile_home=profile)
    append("B says bye", cwd=proj_b, profile_home=profile)
    body_a = read(cwd=proj_a, profile_home=profile)
    body_b = read(cwd=proj_b, profile_home=profile)
    assert "A says hi" in body_a and "B says bye" not in body_a
    assert "B says bye" in body_b and "A says hi" not in body_b


def test_same_project_different_subdirs_share_memory(tmp_path):
    """When cwd is inside a git repo, any subdir resolves to the same
    project id (because we walk up to the repo root)."""
    profile = tmp_path / "profile"
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    sub_a = repo / "src"
    sub_b = repo / "tests"
    sub_a.mkdir()
    sub_b.mkdir()
    append("from src", cwd=sub_a, profile_home=profile)
    body_from_tests = read(cwd=sub_b, profile_home=profile)
    assert "from src" in body_from_tests


# ─── safety ──────────────────────────────────────────────────────────


def test_append_with_unwritable_profile_does_not_raise(tmp_path, monkeypatch):
    """An OSError from mkdir is swallowed; append returns False."""
    profile = tmp_path / "profile"

    def bad_mkdir(*args, **kwargs):
        raise PermissionError("simulated read-only filesystem")

    monkeypatch.setattr(Path, "mkdir", bad_mkdir)
    ok = append("something", cwd=tmp_path, profile_home=profile)
    assert ok is False


def test_read_with_unreadable_file_does_not_raise(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    proj = tmp_path / "myproj"
    proj.mkdir()
    append("setup", cwd=proj, profile_home=profile)

    real_read_text = Path.read_text

    def bad_read_text(self, *args, **kwargs):
        if self.name == "memory.md":
            raise PermissionError("simulated unreadable")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", bad_read_text)
    body = read(cwd=proj, profile_home=profile)
    assert body == ""


def test_locate_with_no_profile_home_uses_default():
    """``profile_home=None`` resolves to ``~/.opencomputer``."""
    loc = locate(cwd=Path("/tmp"))
    # We don't write anything — just verify the path lands under
    # the user's home dir.
    assert ".opencomputer" in str(loc.directory)
    assert "projects" in str(loc.directory)

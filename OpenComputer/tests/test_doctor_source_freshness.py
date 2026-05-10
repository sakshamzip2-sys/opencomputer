"""Tests for ``oc doctor`` source-freshness check + ``oc --version`` git suffix.

The "merged but not deployed" gotcha — pip-editable installs bind to a
filesystem path, not a git branch. Merging to GitHub does NOT update
the user's binary until that specific tree is updated.

The checks here:

1. ``_check_source_freshness`` flags drift between local HEAD and
   ``origin/main`` based on local refs (no network).
2. ``get_source_version_string`` includes the git sha and drift
   counts when running from a git checkout.
3. Both fall back gracefully to "skip" / plain version when running
   from a non-git install (PyPI, wheel).

Tests construct synthetic git repos in tmp_path and patch
:func:`opencomputer.doctor._resolve_source_tree` to point at them, so
no real OpenComputer state is touched.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def _run(cmd: list[str], cwd: Path) -> None:
    """Run a command in cwd; fail loudly on non-zero exit."""
    result = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"command failed: {' '.join(cmd)}\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )


def _make_repo(path: Path, *, with_origin_main: bool = True) -> None:
    """Build a minimal git repo at *path* with one commit on main.

    When ``with_origin_main=True``, also stamps a synthetic
    ``refs/remotes/origin/main`` ref pointing at the same commit, so
    the freshness check can read it without a real remote.
    """
    path.mkdir(parents=True, exist_ok=True)
    _run(["git", "init", "-q", "-b", "main"], cwd=path)
    _run(["git", "config", "user.email", "t@t"], cwd=path)
    _run(["git", "config", "user.name", "t"], cwd=path)
    (path / "README.md").write_text("hi\n")
    _run(["git", "add", "."], cwd=path)
    _run(["git", "commit", "-q", "-m", "initial"], cwd=path)
    if with_origin_main:
        # Synthesize the remote-tracking ref pointing at HEAD.
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=path, capture_output=True, text=True, check=True
        ).stdout.strip()
        (path / ".git" / "refs" / "remotes" / "origin").mkdir(parents=True, exist_ok=True)
        (path / ".git" / "refs" / "remotes" / "origin" / "main").write_text(sha + "\n")


# ─── _check_source_freshness ──────────────────────────────────────────


def test_check_source_freshness_passes_when_head_equals_origin_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HEAD == origin/main → pass."""
    from opencomputer import doctor

    repo = tmp_path / "repo"
    _make_repo(repo, with_origin_main=True)
    monkeypatch.setattr(doctor, "_resolve_source_tree", lambda: repo)

    check = doctor._check_source_freshness()
    assert check.status == "pass", f"detail: {check.detail}"
    assert "HEAD=" in check.detail


def test_check_source_freshness_warns_when_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """origin/main has 1 commit not in HEAD → warn with actionable fix hint."""
    from opencomputer import doctor

    repo = tmp_path / "repo"
    _make_repo(repo, with_origin_main=True)

    # Now advance origin/main one commit ahead of HEAD without touching HEAD.
    # Simulates "user merged a PR upstream but hasn't pulled it locally".
    sha_old = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    (repo / "newfile.md").write_text("merged\n")
    _run(["git", "add", "."], cwd=repo)
    _run(["git", "commit", "-q", "-m", "merge from PR"], cwd=repo)
    sha_new = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    # Advance origin/main pointer to the new commit, then put HEAD back.
    (repo / ".git" / "refs" / "remotes" / "origin" / "main").write_text(sha_new + "\n")
    _run(["git", "reset", "--hard", "-q", sha_old], cwd=repo)

    monkeypatch.setattr(doctor, "_resolve_source_tree", lambda: repo)

    check = doctor._check_source_freshness()
    assert check.status == "warn", f"detail: {check.detail}"
    assert "behind" in check.detail.lower()
    assert "pull" in check.detail.lower(), (
        f"warning must contain actionable 'git pull' hint; got: {check.detail!r}"
    )
    assert "STALE" in check.detail or "stale" in check.detail


def test_check_source_freshness_passes_when_ahead_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HEAD is ahead of origin/main, not behind → pass (feature branch)."""
    from opencomputer import doctor

    repo = tmp_path / "repo"
    _make_repo(repo, with_origin_main=True)
    # Add 2 commits on top of HEAD without touching origin/main.
    for i in range(2):
        (repo / f"feat-{i}.md").write_text(f"feat {i}\n")
        _run(["git", "add", "."], cwd=repo)
        _run(["git", "commit", "-q", "-m", f"feat {i}"], cwd=repo)

    monkeypatch.setattr(doctor, "_resolve_source_tree", lambda: repo)

    check = doctor._check_source_freshness()
    assert check.status == "pass", f"detail: {check.detail}"
    assert "ahead" in check.detail.lower()


def test_check_source_freshness_skips_for_non_git_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PyPI install (no .git ancestor) → skip, not warn."""
    from opencomputer import doctor

    monkeypatch.setattr(doctor, "_resolve_source_tree", lambda: None)

    check = doctor._check_source_freshness()
    assert check.status == "skip"
    assert "PyPI" in check.detail or "wheel" in check.detail or "non-git" in check.detail


def test_check_source_freshness_warns_when_no_origin_main_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """git repo but no origin/main ref (never fetched) → warn with fix hint."""
    from opencomputer import doctor

    repo = tmp_path / "repo"
    _make_repo(repo, with_origin_main=False)
    monkeypatch.setattr(doctor, "_resolve_source_tree", lambda: repo)

    check = doctor._check_source_freshness()
    assert check.status == "warn"
    assert "fetch" in check.detail.lower()


# ─── get_source_version_string ────────────────────────────────────────


def test_version_string_includes_sha_when_in_git_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from opencomputer import __version__, doctor

    repo = tmp_path / "repo"
    _make_repo(repo, with_origin_main=True)
    monkeypatch.setattr(doctor, "_resolve_source_tree", lambda: repo)

    s = doctor.get_source_version_string()
    assert s.startswith(f"opencomputer {__version__}")
    assert "git: " in s
    assert "behind/ahead" in s


def test_version_string_falls_back_to_plain_for_non_git(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opencomputer import __version__, doctor

    monkeypatch.setattr(doctor, "_resolve_source_tree", lambda: None)

    s = doctor.get_source_version_string()
    assert s == f"opencomputer {__version__}"


def test_version_string_never_raises_on_internal_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even when source-tree resolution blows up, version string must work."""
    from opencomputer import __version__, doctor

    def _explode() -> Path:
        raise RuntimeError("simulated git failure")

    monkeypatch.setattr(doctor, "_resolve_source_tree", _explode)

    s = doctor.get_source_version_string()
    assert s == f"opencomputer {__version__}"

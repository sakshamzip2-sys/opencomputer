"""Tests for install_from_git — shallow clone + ref pin + plugin.json id check."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from opencomputer.plugins.remote_install import (
    GitNotFoundError,
    PluginIdMismatchError,
    install_from_git,
)


def _git_available() -> bool:
    return shutil.which("git") is not None


def _seed_local_repo(repo_dir: Path, plugin_id: str = "example") -> str:
    """Create a real local git repo with a plugin.json, return its HEAD sha."""
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo_dir, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "config",
            "commit.gpgsign",
            "false",
        ],
        cwd=repo_dir,
        check=True,
    )

    (repo_dir / "plugin.json").write_text(
        json.dumps(
            {
                "id": plugin_id,
                "name": plugin_id,
                "version": "0.1.0",
                "entry": "plugin.py",
            }
        )
    )
    (repo_dir / "plugin.py").write_text("def register(api):\n    pass\n")

    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-q",
            "-m",
            "init",
        ],
        cwd=repo_dir,
        check=True,
    )
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return sha


@pytest.mark.skipif(not _git_available(), reason="git binary not on PATH")
def test_install_from_local_git_url(tmp_path: Path):
    src = tmp_path / "src-repo"
    head = _seed_local_repo(src, plugin_id="git-example")

    dest_root = tmp_path / "plugins"
    dest_root.mkdir()

    result = install_from_git(
        f"file://{src}",
        dest_root=dest_root,
        plugin_id_hint="git-example",
    )

    assert result.plugin_id == "git-example"
    assert (result.install_path / "plugin.json").exists()
    # Index recorded
    from opencomputer.plugins.installed_index import find_record

    rec = find_record(dest_root / ".installed_index.json", "git-example")
    assert rec is not None
    assert rec.source == "git"
    assert rec.source_ref == head


@pytest.mark.skipif(not _git_available(), reason="git binary not on PATH")
def test_install_from_git_with_explicit_ref(tmp_path: Path):
    src = tmp_path / "src-repo2"
    head = _seed_local_repo(src, plugin_id="ref-example")

    dest_root = tmp_path / "plugins"
    dest_root.mkdir()

    result = install_from_git(
        f"file://{src}",
        dest_root=dest_root,
        plugin_id_hint="ref-example",
        ref=head,
    )
    assert result.plugin_id == "ref-example"


@pytest.mark.skipif(not _git_available(), reason="git binary not on PATH")
def test_install_from_git_id_mismatch_rejected(tmp_path: Path):
    src = tmp_path / "src-repo3"
    _seed_local_repo(src, plugin_id="real-id")

    dest_root = tmp_path / "plugins"
    dest_root.mkdir()

    with pytest.raises(PluginIdMismatchError):
        install_from_git(
            f"file://{src}",
            dest_root=dest_root,
            plugin_id_hint="WRONG-id",
        )


def test_install_from_git_missing_binary_raises(tmp_path: Path):
    with patch("opencomputer.plugins.remote_install._git_path", return_value=None):
        with pytest.raises(GitNotFoundError):
            install_from_git(
                "git+https://github.com/x/y.git",
                dest_root=tmp_path,
                plugin_id_hint="x",
            )

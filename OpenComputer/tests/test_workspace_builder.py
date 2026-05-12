"""Tests for opencomputer.workspace.builder — cache + pnpm invocation."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from opencomputer.workspace.builder import (
    BuildFailed,
    build_workspace,
    is_build_fresh,
    is_install_complete,
)


def _setup_workspace(root: Path, *, with_node_modules: bool = False, with_dist: bool = False) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "package.json").write_text('{"name": "fake"}', encoding="utf-8")
    (root / "server-entry.js").write_text("// fake", encoding="utf-8")
    if with_node_modules:
        nm = root / "node_modules"
        nm.mkdir()
        (nm / ".modules.yaml").write_text("hoistPattern: ['*']\n", encoding="utf-8")
    if with_dist:
        dist = root / "dist" / "server"
        dist.mkdir(parents=True)
        (dist / "server.js").write_text("// fake", encoding="utf-8")
        # Make sure dist mtime > package.json mtime
        time.sleep(0.01)
        (dist / "server.js").touch()
    return root


def test_is_install_complete_true_when_marker_present(tmp_path: Path) -> None:
    ws = _setup_workspace(tmp_path / "ws", with_node_modules=True)
    assert is_install_complete(ws) is True


def test_is_install_complete_false_when_node_modules_missing(tmp_path: Path) -> None:
    ws = _setup_workspace(tmp_path / "ws")
    assert is_install_complete(ws) is False


def test_is_install_complete_false_when_marker_missing(tmp_path: Path) -> None:
    """Half-baked install — node_modules/ exists but no .modules.yaml."""
    ws = _setup_workspace(tmp_path / "ws")
    (ws / "node_modules").mkdir()
    assert is_install_complete(ws) is False


def test_is_build_fresh_true_when_newer(tmp_path: Path) -> None:
    ws = _setup_workspace(tmp_path / "ws", with_dist=True)
    assert is_build_fresh(ws) is True


def test_is_build_fresh_false_when_missing(tmp_path: Path) -> None:
    ws = _setup_workspace(tmp_path / "ws")
    assert is_build_fresh(ws) is False


def test_is_build_fresh_false_when_stale(tmp_path: Path) -> None:
    """When package.json was modified AFTER dist/, dist/ is stale."""
    ws = _setup_workspace(tmp_path / "ws", with_dist=True)
    # Bump package.json mtime to be newer than dist/server/server.js
    time.sleep(0.01)
    (ws / "package.json").touch()
    assert is_build_fresh(ws) is False


def test_build_cache_hit_skips_both_steps(tmp_path: Path) -> None:
    ws = _setup_workspace(tmp_path / "ws", with_node_modules=True, with_dist=True)
    pnpm = tmp_path / "pnpm"
    pnpm.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    pnpm.chmod(0o755)

    with patch("subprocess.run") as mock_run:
        outcome = build_workspace(ws, pnpm_path=str(pnpm), force=False)
    assert mock_run.call_count == 0
    assert not outcome.installed
    assert not outcome.built
    assert outcome.skipped_reason


def test_build_runs_install_when_no_node_modules(tmp_path: Path) -> None:
    ws = _setup_workspace(tmp_path / "ws", with_dist=True)
    pnpm = tmp_path / "pnpm"
    pnpm.write_text("", encoding="utf-8")
    pnpm.chmod(0o755)

    def _run_ok(cmd: list[str], **_: Any) -> Any:
        return subprocess.CompletedProcess(args=cmd, returncode=0)

    with patch("subprocess.run", side_effect=_run_ok) as mock_run:
        outcome = build_workspace(ws, pnpm_path=str(pnpm), force=False)
    # install + build (install invalidates cached build)
    assert mock_run.call_count == 2
    assert outcome.installed
    assert outcome.built


def test_build_failed_raises(tmp_path: Path) -> None:
    ws = _setup_workspace(tmp_path / "ws", with_dist=True)
    pnpm = tmp_path / "pnpm"
    pnpm.write_text("", encoding="utf-8")
    pnpm.chmod(0o755)

    def _run_fail(cmd: list[str], **_: Any) -> Any:
        return subprocess.CompletedProcess(args=cmd, returncode=42)

    with (
        patch("subprocess.run", side_effect=_run_fail),
        pytest.raises(BuildFailed) as exc_info,
    ):
        build_workspace(ws, pnpm_path=str(pnpm), force=False)
    assert exc_info.value.returncode == 42


def test_build_missing_pnpm_raises_filenotfound(tmp_path: Path) -> None:
    ws = _setup_workspace(tmp_path / "ws")
    with pytest.raises(FileNotFoundError, match="pnpm binary not found"):
        build_workspace(ws, pnpm_path=None, force=False)


def test_build_pnpm_path_invalid_raises_filenotfound(tmp_path: Path) -> None:
    ws = _setup_workspace(tmp_path / "ws")
    with pytest.raises(FileNotFoundError, match="pnpm binary not found"):
        build_workspace(ws, pnpm_path=str(tmp_path / "nope"), force=False)


def test_force_rebuilds_everything(tmp_path: Path) -> None:
    ws = _setup_workspace(tmp_path / "ws", with_node_modules=True, with_dist=True)
    pnpm = tmp_path / "pnpm"
    pnpm.write_text("", encoding="utf-8")
    pnpm.chmod(0o755)

    def _run_ok(cmd: list[str], **_: Any) -> Any:
        return subprocess.CompletedProcess(args=cmd, returncode=0)

    with patch("subprocess.run", side_effect=_run_ok) as mock_run:
        outcome = build_workspace(ws, pnpm_path=str(pnpm), force=True)
    assert mock_run.call_count == 2
    assert outcome.installed and outcome.built

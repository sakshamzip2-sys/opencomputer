"""Tests for opencomputer.workspace.prerequisites — node + pnpm probing."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

from opencomputer.workspace.prerequisites import (
    MIN_NODE_MAJOR,
    MIN_PNPM_MAJOR,
    PrerequisiteStatus,
    ToolCheck,
    _parse_major,
    check_prerequisites,
)


def test_parse_major_strips_v_prefix() -> None:
    assert _parse_major("v22.10.1") == 22


def test_parse_major_handles_no_prefix() -> None:
    assert _parse_major("10.23.0") == 10


def test_parse_major_returns_none_on_garbage() -> None:
    assert _parse_major("not a version") is None


def test_parse_major_handles_multiline_output() -> None:
    """pnpm sometimes prefixes notice text before the version."""
    sample = "Some notice text\n9.15.4\n"
    assert _parse_major(sample) == 9


def _fake_run(stdout: str, returncode: int = 0) -> Any:
    """Build a CompletedProcess look-alike for subprocess.run."""
    cp = subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")
    return cp


def test_check_prerequisites_all_present() -> None:
    def _which(name: str) -> str:
        return f"/usr/local/bin/{name}"

    def _run(cmd: list[str], **_: Any) -> Any:
        if cmd[0].endswith("node"):
            return _fake_run("v22.10.1")
        return _fake_run("10.23.0")

    with (
        patch("shutil.which", side_effect=_which),
        patch("subprocess.run", side_effect=_run),
    ):
        status = check_prerequisites()
    assert status.ok
    assert status.node.ok and status.pnpm.ok
    assert "22.10.1" in status.node.version
    assert "10.23.0" in status.pnpm.version


def test_check_prerequisites_node_missing() -> None:
    def _which(name: str) -> str | None:
        return None if name == "node" else "/usr/local/bin/pnpm"

    def _run(cmd: list[str], **_: Any) -> Any:
        return _fake_run("10.23.0")

    with (
        patch("shutil.which", side_effect=_which),
        patch("subprocess.run", side_effect=_run),
    ):
        status = check_prerequisites()
    assert not status.ok
    assert not status.node.ok
    assert "not found on PATH" in status.node.detail
    # pnpm is fine; only node is reported missing.
    assert status.pnpm.ok


def test_check_prerequisites_node_too_old() -> None:
    def _which(_: str) -> str:
        return "/usr/local/bin/node"

    def _run(cmd: list[str], **_: Any) -> Any:
        return _fake_run("v18.0.0")  # below minimum

    with (
        patch("shutil.which", side_effect=_which),
        patch("subprocess.run", side_effect=_run),
    ):
        status = check_prerequisites()
    assert not status.ok
    assert not status.node.ok
    assert f"below required major {MIN_NODE_MAJOR}" in status.node.detail


def test_check_prerequisites_pnpm_too_old() -> None:
    def _which(name: str) -> str:
        return f"/usr/local/bin/{name}"

    def _run(cmd: list[str], **_: Any) -> Any:
        if cmd[0].endswith("node"):
            return _fake_run("v22.10.0")
        return _fake_run("8.15.0")  # pnpm below minimum

    with (
        patch("shutil.which", side_effect=_which),
        patch("subprocess.run", side_effect=_run),
    ):
        status = check_prerequisites()
    assert not status.ok
    assert status.node.ok
    assert not status.pnpm.ok
    assert f"below required major {MIN_PNPM_MAJOR}" in status.pnpm.detail


def test_check_prerequisites_timeout() -> None:
    def _which(_: str) -> str:
        return "/usr/local/bin/node"

    def _run(cmd: list[str], **_: Any) -> Any:
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=10.0)

    with (
        patch("shutil.which", side_effect=_which),
        patch("subprocess.run", side_effect=_run),
    ):
        status = check_prerequisites()
    assert not status.ok
    assert "timed out" in status.node.detail


def test_check_prerequisites_garbage_version_output() -> None:
    def _which(_: str) -> str:
        return "/usr/local/bin/x"

    def _run(cmd: list[str], **_: Any) -> Any:
        return _fake_run("hello world")

    with (
        patch("shutil.which", side_effect=_which),
        patch("subprocess.run", side_effect=_run),
    ):
        status = check_prerequisites()
    assert not status.ok
    assert "could not parse" in status.node.detail


def test_status_report_lines_includes_fix_hints() -> None:
    node_check = ToolCheck(
        name="node",
        path=None,
        version=None,
        ok=False,
        detail="missing",
    )
    pnpm_check = ToolCheck(
        name="pnpm",
        path=None,
        version=None,
        ok=False,
        detail="missing",
    )
    status = PrerequisiteStatus(node=node_check, pnpm=pnpm_check)
    text = "\n".join(status.report_lines())
    assert "Install Node.js" in text
    assert "Install pnpm" in text
    assert "MISSING" in text

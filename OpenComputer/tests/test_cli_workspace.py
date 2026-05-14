"""Smoke tests for the ``oc workspace`` Typer surface."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from opencomputer.cli_workspace import workspace_app

runner = CliRunner()


def _stub_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "fake-ws"
    ws.mkdir()
    (ws / "package.json").write_text('{"name":"fake"}', encoding="utf-8")
    (ws / "server-entry.js").write_text("// fake", encoding="utf-8")
    return ws


def test_workspace_help_lists_subcommands() -> None:
    result = runner.invoke(workspace_app, ["--help"])
    assert result.exit_code == 0
    assert "run" in result.output
    assert "build" in result.output
    assert "doctor" in result.output


def test_workspace_doctor_reports_missing_workspace_dir(
    tmp_path: Path, monkeypatch
) -> None:
    # OC_WORKSPACE_DIR short-circuits all other candidates in
    # discover_workspace_dir, so clear it explicitly — otherwise a
    # developer with the env var set in their shell sees the test go
    # green even when no fallback would have worked.
    monkeypatch.delenv("OC_WORKSPACE_DIR", raising=False)
    with patch(
        "opencomputer.cli_workspace._resolve_profile_home",
        return_value=tmp_path / "no-profile",
    ):
        # Point Path.home() at nothing too so the global candidate misses.
        orig_home = Path.home
        try:
            Path.home = staticmethod(lambda: tmp_path / "no-home")  # type: ignore[assignment]
            with patch(
                "opencomputer.workspace.discovery.DEFAULT_DEV_SOURCES_PATH",
                tmp_path / "no-dev",
            ):
                result = runner.invoke(workspace_app, ["doctor"])
        finally:
            Path.home = orig_home  # type: ignore[assignment]
    assert result.exit_code == 1
    assert "MISSING" in result.output or "workspace dir" in result.output


def test_workspace_doctor_reports_ok_state(tmp_path: Path) -> None:
    ws = _stub_workspace(tmp_path)
    # Pretend node + pnpm exist with valid versions.
    from opencomputer.workspace.prerequisites import (
        PrerequisiteStatus,
        ToolCheck,
    )

    ok_status = PrerequisiteStatus(
        node=ToolCheck(
            name="node",
            path="/fake/node",
            version="v22.10.0",
            ok=True,
            detail="ok",
        ),
        pnpm=ToolCheck(
            name="pnpm",
            path="/fake/pnpm",
            version="9.15.0",
            ok=True,
            detail="ok",
        ),
    )
    with (
        patch(
            "opencomputer.cli_workspace.check_prerequisites",
            return_value=ok_status,
        ),
        patch(
            "opencomputer.cli_workspace._resolve_profile_home",
            return_value=tmp_path / "profile",
        ),
    ):
        result = runner.invoke(
            workspace_app,
            ["doctor", "--workspace-dir", str(ws)],
        )
    # Build state will be "NOT INSTALLED" / "MISSING" → exit 1 — but the
    # doctor itself must have run successfully and printed the workspace
    # path (basename, since Rich may wrap long tmp paths in tests).
    assert ws.name in result.output  # "fake-ws"
    assert "node" in result.output
    assert "pnpm" in result.output


def test_workspace_build_fails_loudly_when_prereqs_missing(tmp_path: Path) -> None:
    ws = _stub_workspace(tmp_path)
    from opencomputer.workspace.prerequisites import (
        PrerequisiteStatus,
        ToolCheck,
    )

    bad = PrerequisiteStatus(
        node=ToolCheck(
            name="node",
            path=None,
            version=None,
            ok=False,
            detail="missing",
        ),
        pnpm=ToolCheck(
            name="pnpm",
            path=None,
            version=None,
            ok=False,
            detail="missing",
        ),
    )
    with patch(
        "opencomputer.cli_workspace.check_prerequisites",
        return_value=bad,
    ):
        result = runner.invoke(
            workspace_app,
            ["build", "--workspace-dir", str(ws)],
        )
    assert result.exit_code == 1
    assert "missing prerequisites" in result.output


def test_workspace_build_invokes_builder_on_success(tmp_path: Path) -> None:
    ws = _stub_workspace(tmp_path)
    from opencomputer.workspace.builder import BuildOutcome
    from opencomputer.workspace.prerequisites import (
        PrerequisiteStatus,
        ToolCheck,
    )

    ok = PrerequisiteStatus(
        node=ToolCheck("node", "/n", "v22", True, "ok"),
        pnpm=ToolCheck("pnpm", "/p", "9", True, "ok"),
    )
    outcome = BuildOutcome(
        installed=True, built=True, skipped_reason=None, elapsed_seconds=1.0,
    )
    with (
        patch(
            "opencomputer.cli_workspace.check_prerequisites",
            return_value=ok,
        ),
        patch(
            "opencomputer.cli_workspace.build_workspace",
            return_value=outcome,
        ),
    ):
        result = runner.invoke(
            workspace_app,
            ["build", "--workspace-dir", str(ws)],
        )
    assert result.exit_code == 0
    assert "✓" in result.output or "pnpm" in result.output

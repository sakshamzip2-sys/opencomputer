"""Tests for `opencomputer mcp scaffold` (G.30 / Tier 4).

The scaffolder generates a small Python MCP server package on disk.
Verify the generated layout, file contents, and CLI ergonomics.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from opencomputer.cli_mcp import mcp_app

runner = CliRunner()


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------


class TestScaffoldedLayout:
    def test_creates_expected_files(self, tmp_path: Path) -> None:
        result = runner.invoke(
            mcp_app, ["scaffold", "stocks-mcp", "--dir", str(tmp_path)]
        )
        assert result.exit_code == 0, result.stdout
        out = tmp_path / "stocks-mcp"
        assert out.is_dir()
        assert (out / "pyproject.toml").is_file()
        assert (out / "README.md").is_file()
        # Hyphenated folder name → underscored package.
        assert (out / "stocks_mcp" / "__init__.py").is_file()
        assert (out / "stocks_mcp" / "server.py").is_file()

    def test_underscore_name_kept_as_is(self, tmp_path: Path) -> None:
        result = runner.invoke(
            mcp_app, ["scaffold", "alpha_mcp", "--dir", str(tmp_path)]
        )
        assert result.exit_code == 0, result.stdout
        # No hyphens → folder == package.
        assert (tmp_path / "alpha_mcp" / "alpha_mcp" / "server.py").is_file()


# ---------------------------------------------------------------------------
# File contents
# ---------------------------------------------------------------------------


class TestScaffoldedContents:
    def test_server_py_uses_fastmcp(self, tmp_path: Path) -> None:
        runner.invoke(
            mcp_app, ["scaffold", "demo-mcp", "--dir", str(tmp_path)]
        )
        body = (tmp_path / "demo-mcp" / "demo_mcp" / "server.py").read_text()
        assert "from mcp.server.fastmcp import FastMCP" in body
        assert 'FastMCP(name="demo_mcp")' in body
        assert "@server.tool()" in body
        # Demo tool exists.
        assert "def echo(text: str) -> str" in body

    def test_pyproject_declares_dependency_and_script(
        self, tmp_path: Path
    ) -> None:
        runner.invoke(
            mcp_app, ["scaffold", "demo-mcp", "--dir", str(tmp_path)]
        )
        body = (tmp_path / "demo-mcp" / "pyproject.toml").read_text()
        assert 'name = "demo-mcp"' in body
        assert "mcp>=1.0" in body
        assert 'demo-mcp = "demo_mcp.server:main"' in body

    def test_readme_includes_register_command(
        self, tmp_path: Path
    ) -> None:
        runner.invoke(
            mcp_app, ["scaffold", "demo-mcp", "--dir", str(tmp_path)]
        )
        body = (tmp_path / "demo-mcp" / "README.md").read_text()
        # Should give a runnable register command.
        assert "opencomputer mcp add demo-mcp" in body
        assert "python -m demo_mcp.server" in body

    def test_transport_propagates(self, tmp_path: Path) -> None:
        runner.invoke(
            mcp_app, ["scaffold", "demo-mcp", "-d", str(tmp_path), "--transport", "sse"],
        )
        body = (tmp_path / "demo-mcp" / "demo_mcp" / "server.py").read_text()
        assert 'server.run(transport="sse")' in body


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestScaffoldValidation:
    def test_invalid_transport_rejected(self, tmp_path: Path) -> None:
        result = runner.invoke(
            mcp_app,
            ["scaffold", "demo", "-d", str(tmp_path), "--transport", "telepathy"],
        )
        assert result.exit_code != 0
        assert "transport" in result.stdout.lower()

    def test_invalid_name_rejected(self, tmp_path: Path) -> None:
        # Path separators should be rejected — security + ergonomics.
        result = runner.invoke(
            mcp_app, ["scaffold", "../escape", "-d", str(tmp_path)]
        )
        assert result.exit_code != 0

    def test_existing_dir_rejected_without_force(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "demo-mcp"
        target.mkdir()
        (target / "PROTECT_ME.txt").write_text("don't trample")
        result = runner.invoke(
            mcp_app, ["scaffold", "demo-mcp", "-d", str(tmp_path)]
        )
        assert result.exit_code != 0
        # File preserved.
        assert (target / "PROTECT_ME.txt").exists()

    def test_force_overwrites(self, tmp_path: Path) -> None:
        target = tmp_path / "demo-mcp"
        target.mkdir()
        (target / "stale.txt").write_text("old")
        result = runner.invoke(
            mcp_app, ["scaffold", "demo-mcp", "-d", str(tmp_path), "--force"]
        )
        assert result.exit_code == 0, result.stdout
        # New layout exists alongside the stale file (force overwrites
        # rather than wipes — that matches the user's mental model of
        # "drop the new files in").
        assert (target / "demo_mcp" / "server.py").is_file()


# ---------------------------------------------------------------------------
# Compile-check the scaffold
# ---------------------------------------------------------------------------


class TestScaffoldCompiles:
    def test_generated_server_py_is_valid_python(self, tmp_path: Path) -> None:
        runner.invoke(
            mcp_app, ["scaffold", "demo-mcp", "--dir", str(tmp_path)]
        )
        path = tmp_path / "demo-mcp" / "demo_mcp" / "server.py"
        body = path.read_text()
        # ``compile`` raises SyntaxError if the scaffold is broken.
        compile(body, str(path), "exec")

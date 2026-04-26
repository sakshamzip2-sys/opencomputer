"""Tests for opencomputer.mcp.presets — bundled preset registry + install CLI.

G.7 / Tier 2.4: vetted one-line installs for common MCPs (filesystem,
github, fetch, postgres, brave-search).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from opencomputer.agent.config import MCPServerConfig
from opencomputer.mcp.presets import PRESETS, Preset, get_preset, list_preset_slugs


@pytest.fixture(autouse=True)
def isolate_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    return tmp_path


class TestPresetRegistry:
    def test_original_five_presets_still_present(self) -> None:
        # The original five slugs from v0.1.x must remain — third-party
        # tooling pinned to ``mcp install <slug>`` would break otherwise.
        # Round 4 expanded the catalog to ≥15 entries; the assertion is
        # now a subset check rather than equality.
        slugs = set(list_preset_slugs())
        original = {"filesystem", "github", "fetch", "postgres", "brave-search"}
        assert original <= slugs, (
            f"original presets removed; backwards compat broken. Missing: "
            f"{original - slugs}"
        )
        assert len(slugs) >= 15, (
            f"Round 4 catalog expansion expects ≥15 entries; got {len(slugs)}"
        )

    def test_each_preset_has_a_command(self) -> None:
        for slug, p in PRESETS.items():
            assert p.config.command, f"{slug} has empty command"
            assert p.config.transport == "stdio", f"{slug} should be stdio (got {p.config.transport!r})"

    def test_each_preset_has_description_and_homepage(self) -> None:
        for slug, p in PRESETS.items():
            assert len(p.description) > 30, f"{slug} description too short"
            assert p.homepage.startswith("https://"), f"{slug} missing homepage"

    def test_get_preset_unknown_returns_none(self) -> None:
        assert get_preset("nonsense") is None

    def test_get_preset_known(self) -> None:
        p = get_preset("filesystem")
        assert isinstance(p, Preset)
        assert p.slug == "filesystem"
        assert p.config.name == "filesystem"

    def test_required_env_for_external_apis(self) -> None:
        # GitHub, postgres, brave-search need credentials
        assert "GITHUB_PERSONAL_ACCESS_TOKEN" in PRESETS["github"].required_env
        assert "POSTGRES_URL" in PRESETS["postgres"].required_env
        assert "BRAVE_API_KEY" in PRESETS["brave-search"].required_env
        # filesystem + fetch don't need creds
        assert PRESETS["filesystem"].required_env == ()
        assert PRESETS["fetch"].required_env == ()

    def test_each_config_is_immutable_dataclass(self) -> None:
        for p in PRESETS.values():
            with pytest.raises((AttributeError, Exception)):  # frozen=True
                p.config.name = "modified"  # type: ignore[misc]


class TestInstallCLI:
    """End-to-end test of `opencomputer mcp install <preset>` via CliRunner."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        return CliRunner()

    def test_presets_command_lists_five(self, runner: CliRunner) -> None:
        from opencomputer.cli_mcp import mcp_app

        result = runner.invoke(mcp_app, ["presets"])
        assert result.exit_code == 0
        for slug in PRESETS:
            assert slug in result.stdout

    def test_install_unknown_preset_errors(self, runner: CliRunner) -> None:
        from opencomputer.cli_mcp import mcp_app

        result = runner.invoke(mcp_app, ["install", "nonsense"])
        assert result.exit_code == 1
        assert "unknown preset" in result.stdout

    def test_install_filesystem_writes_config(self, runner: CliRunner, tmp_path: Path) -> None:
        from opencomputer.cli_mcp import mcp_app

        result = runner.invoke(mcp_app, ["install", "filesystem"])
        assert result.exit_code == 0
        cfg_path = tmp_path / "config.yaml"
        assert cfg_path.exists()
        content = cfg_path.read_text()
        # Spot-check the YAML
        assert "filesystem" in content
        assert "@modelcontextprotocol/server-filesystem" in content

    def test_install_with_custom_name(self, runner: CliRunner, tmp_path: Path) -> None:
        from opencomputer.cli_mcp import mcp_app

        result = runner.invoke(mcp_app, ["install", "filesystem", "--name", "my-fs"])
        assert result.exit_code == 0
        content = (tmp_path / "config.yaml").read_text()
        assert "my-fs" in content

    def test_install_disabled_flag(self, runner: CliRunner, tmp_path: Path) -> None:
        from opencomputer.cli_mcp import mcp_app

        result = runner.invoke(mcp_app, ["install", "filesystem", "--disabled"])
        assert result.exit_code == 0

    def test_install_duplicate_name_errors(self, runner: CliRunner) -> None:
        from opencomputer.cli_mcp import mcp_app

        # First install succeeds
        runner.invoke(mcp_app, ["install", "filesystem"])
        # Second install (same name) should fail
        result2 = runner.invoke(mcp_app, ["install", "filesystem"])
        assert result2.exit_code == 1
        assert "already exists" in result2.stdout

    def test_install_github_warns_when_env_unset(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from opencomputer.cli_mcp import mcp_app

        monkeypatch.delenv("GITHUB_PERSONAL_ACCESS_TOKEN", raising=False)
        result = runner.invoke(mcp_app, ["install", "github"])
        assert result.exit_code == 0
        # Should warn about the missing env var
        assert "GITHUB_PERSONAL_ACCESS_TOKEN" in result.stdout
        assert "unset" in result.stdout.lower() or "missing" in result.stdout.lower()


class TestPresetConfigShape:
    """Each preset's MCPServerConfig should be valid and load-balanced."""

    def test_all_presets_have_valid_args_tuple(self) -> None:
        for slug, p in PRESETS.items():
            assert isinstance(p.config.args, tuple), f"{slug}: args must be tuple (was {type(p.config.args)})"

    def test_all_presets_round_trip_via_dataclass(self) -> None:
        # Verify we can copy the config without errors (frozen=True behaves)
        for p in PRESETS.values():
            new_cfg = MCPServerConfig(
                name=p.config.name,
                transport=p.config.transport,
                command=p.config.command,
                args=p.config.args,
                url=p.config.url,
                env=dict(p.config.env),
                headers=dict(p.config.headers),
                enabled=True,
            )
            assert new_cfg.name == p.config.name

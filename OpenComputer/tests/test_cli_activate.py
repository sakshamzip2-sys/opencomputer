"""M2: `oc activate` wizard scaffolds dormant features into a fresh profile.

Tests the wizard end-to-end with --accept-defaults so all 5 sub-areas
write their starter content; then verifies idempotence (a second run is
a no-op).
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from opencomputer.cli import app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def fresh_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Empty profile dir + isolated $HOME so presets land in a tmp area too."""
    profile_home = tmp_path / "profile"
    profile_home.mkdir()
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(profile_home))
    monkeypatch.setenv("HOME", str(tmp_path / "user_home"))
    (tmp_path / "user_home").mkdir()
    return profile_home


def test_activate_all_writes_each_sub_area(
    runner: CliRunner, fresh_profile: Path, tmp_path: Path
) -> None:
    """`oc activate --accept-defaults` writes every sub-area's starter."""
    result = runner.invoke(app, ["activate", "--accept-defaults"])
    assert result.exit_code == 0, result.stdout

    # 1. MCP — examples written to mcp_examples.yaml (NOT config.yaml).
    # 2026-05-10: writing to config.yaml was unstable because any
    # subsequent set_value call (oc memory dream-on, etc.) re-serializes
    # the dataclass and wipes raw comment blocks. The examples now live
    # in a sibling file users copy from manually.
    examples_yaml = fresh_profile / "mcp_examples.yaml"
    assert examples_yaml.exists(), "mcp_examples.yaml not written"
    assert "MCP servers — uncomment one or more" in examples_yaml.read_text()

    # 2. Agents — 3 starter templates dropped
    agents_dir = fresh_profile / "agents"
    assert (agents_dir / "test-writer.md").exists()
    assert (agents_dir / "doc-writer.md").exists()
    assert (agents_dir / "planner.md").exists()
    # And the frontmatter parses as expected
    test_writer = (agents_dir / "test-writer.md").read_text()
    assert "name: test-writer" in test_writer

    # 3. Bindings — default-only
    bindings_yaml = fresh_profile / "bindings.yaml"
    assert bindings_yaml.exists()
    parsed = yaml.safe_load(bindings_yaml.read_text())
    assert parsed["default_profile"] == "default"
    assert parsed["bindings"] == []

    # 4. Presets — minimal preset in $HOME/.opencomputer/presets/
    presets_dir = tmp_path / "user_home" / ".opencomputer" / "presets"
    assert (presets_dir / "minimal.yaml").exists()
    minimal = yaml.safe_load((presets_dir / "minimal.yaml").read_text())
    assert minimal == {"plugins": []}

    # 5. Rules — no-env-writes.md
    rules_dir = fresh_profile / "rules"
    assert (rules_dir / "no-env-writes.md").exists()
    rule_body = (rules_dir / "no-env-writes.md").read_text()
    assert ".env" in rule_body
    assert "paths:" in rule_body


def test_activate_is_idempotent(
    runner: CliRunner, fresh_profile: Path
) -> None:
    """Second run on a populated profile must skip every sub-area."""
    runner.invoke(app, ["activate", "--accept-defaults"])
    result2 = runner.invoke(app, ["activate", "--accept-defaults"])
    assert result2.exit_code == 0, result2.stdout
    out = result2.stdout
    # Each sub-area reports skip
    assert "skip" in out


def test_activate_subcommand_only_runs_target_area(
    runner: CliRunner, fresh_profile: Path
) -> None:
    """`oc activate bindings -y` only writes bindings.yaml, leaves other areas alone."""
    result = runner.invoke(app, ["activate", "bindings", "--accept-defaults"])
    assert result.exit_code == 0, result.stdout
    assert (fresh_profile / "bindings.yaml").exists()
    # Other areas were NOT touched
    assert not (fresh_profile / "rules").exists() or not list(
        (fresh_profile / "rules").glob("*.md")
    )


def test_activate_mcp_skips_when_servers_already_configured(
    runner: CliRunner, fresh_profile: Path
) -> None:
    """If config.yaml already has mcp.servers, the wizard refuses to overwrite."""
    config = fresh_profile / "config.yaml"
    config.write_text(
        yaml.safe_dump(
            {"mcp": {"servers": [{"name": "existing", "transport": "stdio", "command": "echo"}]}}
        )
    )
    result = runner.invoke(app, ["activate", "mcp", "--accept-defaults"])
    assert result.exit_code == 0
    assert "skip" in result.stdout.lower() or "already configured" in result.stdout.lower()


def test_activate_agents_skips_when_already_present(
    runner: CliRunner, fresh_profile: Path
) -> None:
    """If <profile>/agents/ already has all 3 starter templates, do nothing."""
    agents_dir = fresh_profile / "agents"
    agents_dir.mkdir()
    for name in ("test-writer.md", "doc-writer.md", "planner.md"):
        (agents_dir / name).write_text("---\nname: x\n---\nbody")
    result = runner.invoke(app, ["activate", "agents", "--accept-defaults"])
    assert result.exit_code == 0
    out = result.stdout.lower()
    assert "skip" in out or "already present" in out

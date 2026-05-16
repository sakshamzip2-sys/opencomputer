"""CLI tests for the Milestone-1 ``oc sandbox`` scope surface.

Covers ``enable`` / ``disable`` and the dual-mode ``explain`` added in T1.4.
Each test re-roots the profile home at a ``tmp_path`` via ``set_profile`` so
``oc sandbox enable`` writes a throwaway ``config.yaml`` instead of the real
profile.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from opencomputer.agent.config_store import load_config
from opencomputer.cli_sandbox import sandbox_app
from opencomputer.sandbox.policy import SandboxPolicy, SandboxScope
from plugin_sdk.profile_context import set_profile

runner = CliRunner()


def test_explain_bare_shows_disabled_policy_by_default(tmp_path: Path) -> None:
    with set_profile(tmp_path):
        result = runner.invoke(sandbox_app, ["explain"])
    assert result.exit_code == 0, result.output
    assert "Sandbox policy" in result.output
    assert "none" in result.output


def test_enable_persists_scope_to_config(tmp_path: Path) -> None:
    with set_profile(tmp_path):
        result = runner.invoke(sandbox_app, ["enable", "--scope", "session"])
        assert result.exit_code == 0, result.output
        assert load_config().sandbox.scope is SandboxScope.SESSION
    assert "sandbox enabled" in result.output


def test_enable_defaults_to_session_scope(tmp_path: Path) -> None:
    with set_profile(tmp_path):
        result = runner.invoke(sandbox_app, ["enable"])
        assert result.exit_code == 0, result.output
        assert load_config().sandbox.scope is SandboxScope.SESSION


def test_explain_reflects_enabled_scope(tmp_path: Path) -> None:
    with set_profile(tmp_path):
        runner.invoke(sandbox_app, ["enable", "--scope", "shared"])
        result = runner.invoke(sandbox_app, ["explain"])
    assert result.exit_code == 0, result.output
    assert "shared" in result.output
    assert "yes" in result.output  # enabled


def test_disable_sets_scope_back_to_none(tmp_path: Path) -> None:
    with set_profile(tmp_path):
        runner.invoke(sandbox_app, ["enable", "--scope", "agent"])
        result = runner.invoke(sandbox_app, ["disable"])
        assert result.exit_code == 0, result.output
        assert load_config().sandbox.scope is SandboxScope.NONE
    assert "sandbox disabled" in result.output


def test_enable_rejects_scope_none(tmp_path: Path) -> None:
    with set_profile(tmp_path):
        result = runner.invoke(sandbox_app, ["enable", "--scope", "none"])
    assert result.exit_code == 2
    assert "disable" in result.output


def test_enable_rejects_unknown_scope(tmp_path: Path) -> None:
    with set_profile(tmp_path):
        result = runner.invoke(sandbox_app, ["enable", "--scope", "bogus"])
    assert result.exit_code == 2
    assert "bogus" in result.output


def test_enable_preserves_existing_tool_lists(tmp_path: Path) -> None:
    """Switching scope must not drop a hand-configured tools.deny list."""
    (tmp_path / "config.yaml").write_text(
        "sandbox:\n  scope: tool\n  tools:\n    deny: [Bash]\n"
    )
    with set_profile(tmp_path):
        result = runner.invoke(sandbox_app, ["enable", "--scope", "agent"])
        assert result.exit_code == 0, result.output
        reloaded = load_config().sandbox
    assert reloaded.scope is SandboxScope.AGENT
    assert reloaded.tools_deny == ("Bash",)


def test_explain_with_argv_prints_wrapped_command(tmp_path: Path) -> None:
    """``oc sandbox explain -- <argv>`` keeps the original dry-run behavior."""
    with set_profile(tmp_path):
        result = runner.invoke(sandbox_app, ["explain", "--", "echo", "hi"])
    assert result.exit_code == 0, result.output
    assert "echo" in result.output


def test_disable_when_already_disabled_is_a_noop(tmp_path: Path) -> None:
    with set_profile(tmp_path):
        result = runner.invoke(sandbox_app, ["disable"])
    assert result.exit_code == 0, result.output
    assert "already disabled" in result.output


def test_status_still_works(tmp_path: Path) -> None:
    """The pre-existing ``status`` command is unaffected by the M1 additions."""
    with set_profile(tmp_path):
        result = runner.invoke(sandbox_app, ["status"])
    assert result.exit_code == 0, result.output
    for name in ("macos_sandbox_exec", "linux_bwrap", "docker", "none"):
        assert name in result.output


def test_enable_then_disable_round_trips_policy(tmp_path: Path) -> None:
    with set_profile(tmp_path):
        runner.invoke(sandbox_app, ["enable", "--scope", "session"])
        runner.invoke(sandbox_app, ["disable"])
        assert load_config().sandbox == SandboxPolicy()  # exact default restored

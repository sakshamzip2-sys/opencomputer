"""Tests for the Skills Hub CLI surface.

Tests run against the existing ``cli_skills.app`` (which has hub commands
attached via ``attach_hub_commands``). Confirms hub commands coexist with
the evolution review commands without collision.
"""
from typer.testing import CliRunner

from opencomputer.cli_skills import app

runner = CliRunner()


def test_search_command_renders_results(monkeypatch, tmp_path):
    """oc skills search hits well-known and renders rows."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    result = runner.invoke(app, ["search", ""])
    assert result.exit_code == 0


def test_browse_alias_for_empty_search(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    r = runner.invoke(app, ["browse"])
    assert r.exit_code == 0


def test_inspect_known_identifier_succeeds(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    r = runner.invoke(app, ["inspect", "well-known/api-design"])
    assert r.exit_code == 0
    assert "api-design" in r.stdout


def test_inspect_unknown_identifier_nonzero(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    r = runner.invoke(app, ["inspect", "well-known/nope-xyzzy"])
    assert r.exit_code != 0


def test_install_then_installed(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    r1 = runner.invoke(app, ["install", "well-known/api-design", "--yes"])
    assert r1.exit_code == 0, r1.stdout
    r2 = runner.invoke(app, ["installed"])
    assert r2.exit_code == 0
    assert "api-design" in r2.stdout


def test_install_then_uninstall(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    runner.invoke(app, ["install", "well-known/api-design", "--yes"])
    r = runner.invoke(app, ["uninstall", "well-known/api-design", "--yes"])
    assert r.exit_code == 0
    r2 = runner.invoke(app, ["installed"])
    assert "api-design" not in r2.stdout


def test_audit_shows_install_event(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    runner.invoke(app, ["install", "well-known/api-design", "--yes"])
    r = runner.invoke(app, ["audit"])
    assert r.exit_code == 0
    assert "install" in r.stdout


def test_search_unknown_source_shows_helpful_error(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    r = runner.invoke(app, ["search", "", "--source", "definitely-not-real"])
    assert "Unknown source" in r.stdout


def test_existing_evolution_list_command_still_works(monkeypatch, tmp_path):
    """Sanity: hub command attachment did not break existing 'list' command."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    r = runner.invoke(app, ["list"])
    # Evolution list may exit 0 or non-zero based on whether evolution
    # is enabled; the important thing is it didn't fail to dispatch
    # (which would mean argparse couldn't find the command).
    # If it did dispatch, exit_code is from the command logic itself.
    assert "no such command" not in r.stdout.lower()


def test_skills_help_lists_both_evolution_and_hub_commands(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    r = runner.invoke(app, ["--help"])
    assert r.exit_code == 0
    # Evolution commands
    assert "list" in r.stdout
    assert "accept" in r.stdout
    # Hub commands
    assert "search" in r.stdout
    assert "install" in r.stdout
    assert "installed" in r.stdout


def test_update_command_flow(monkeypatch, tmp_path):
    """Update = uninstall + reinstall."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    runner.invoke(app, ["install", "well-known/api-design", "--yes"])
    r = runner.invoke(app, ["update", "well-known/api-design", "--yes"])
    assert r.exit_code == 0
    # Should still be in installed list
    r2 = runner.invoke(app, ["installed"])
    assert "api-design" in r2.stdout


def test_audit_with_action_filter(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    runner.invoke(app, ["install", "well-known/api-design", "--yes"])
    runner.invoke(app, ["uninstall", "well-known/api-design", "--yes"])
    r_inst = runner.invoke(app, ["audit", "--action", "install"])
    r_uninst = runner.invoke(app, ["audit", "--action", "uninstall"])
    assert "install" in r_inst.stdout
    assert "uninstall" in r_uninst.stdout

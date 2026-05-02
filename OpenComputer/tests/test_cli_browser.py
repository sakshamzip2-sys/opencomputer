"""oc browser CLI dispatch."""
from pathlib import Path

from typer.testing import CliRunner

from opencomputer.cli_browser import browser_app


def test_browser_help():
    runner = CliRunner()
    result = runner.invoke(browser_app, ["--help"])
    assert result.exit_code == 0
    assert "list" in result.stdout
    assert "show" in result.stdout
    assert "chrome" in result.stdout


def test_browser_list_returns_recipes(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_RECIPES_PROFILE_DIR", str(tmp_path / "profile"))
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    (bundled / "alpha.yaml").write_text(
        "name: alpha\ncommands:\n  go:\n    pipeline:\n      - fetch: 'https://x'\n"
    )
    monkeypatch.setenv("OPENCOMPUTER_RECIPES_BUNDLED_DIR", str(bundled))

    runner = CliRunner()
    result = runner.invoke(browser_app, ["list"])
    assert result.exit_code == 0
    assert "alpha" in result.stdout


def test_browser_show_prints_recipe_info(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_RECIPES_PROFILE_DIR", str(tmp_path / "profile"))
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    (bundled / "alpha.yaml").write_text(
        "name: alpha\n"
        "description: alpha description\n"
        "commands:\n"
        "  go:\n"
        "    description: do the thing\n"
        "    pipeline:\n"
        "      - fetch: 'https://x'\n"
    )
    monkeypatch.setenv("OPENCOMPUTER_RECIPES_BUNDLED_DIR", str(bundled))

    runner = CliRunner()
    result = runner.invoke(browser_app, ["show", "alpha"])
    assert result.exit_code == 0
    assert "alpha" in result.stdout
    assert "go" in result.stdout
    assert "fetch" in result.stdout


def test_browser_show_unknown_site_exits_1(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_RECIPES_PROFILE_DIR", str(tmp_path / "profile"))
    monkeypatch.setenv("OPENCOMPUTER_RECIPES_BUNDLED_DIR", str(tmp_path / "bundled"))

    runner = CliRunner()
    result = runner.invoke(browser_app, ["show", "missing"])
    assert result.exit_code == 1


def test_browser_chrome_prints_command():
    runner = CliRunner()
    result = runner.invoke(browser_app, ["chrome"])
    assert result.exit_code == 0
    assert "--remote-debugging-port=9222" in result.stdout
    assert "OPENCOMPUTER_BROWSER_CDP_URL" in result.stdout


def test_browser_run_missing_recipe_exits_1(tmp_path, monkeypatch):
    """Default behaviour when no recipe matches: exit 1 with helpful message."""
    monkeypatch.setenv("OPENCOMPUTER_RECIPES_PROFILE_DIR", str(tmp_path / "profile"))
    monkeypatch.setenv("OPENCOMPUTER_RECIPES_BUNDLED_DIR", str(tmp_path / "bundled"))

    runner = CliRunner()
    result = runner.invoke(browser_app, ["run", "nonexistent", "verb"])
    assert result.exit_code == 1


def test_browser_run_with_llm_fallback_exits_2_v1_stub(tmp_path, monkeypatch):
    """v1: --llm-fallback is a stub; exits 2 with 'not yet implemented' message."""
    monkeypatch.setenv("OPENCOMPUTER_RECIPES_PROFILE_DIR", str(tmp_path / "profile"))
    monkeypatch.setenv("OPENCOMPUTER_RECIPES_BUNDLED_DIR", str(tmp_path / "bundled"))

    runner = CliRunner()
    result = runner.invoke(
        browser_app, ["run", "nonexistent", "verb", "--llm-fallback"]
    )
    assert result.exit_code == 2

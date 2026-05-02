"""github_trending recipe loads + runs against a mock HTML fetcher."""
from pathlib import Path

import pytest

from opencomputer.recipes import load_recipe, run_recipe

SAMPLE_TRENDING_HTML = """
<html><body>
  <article class="Box-row">
    <h2><a href="/anthropics/claude-code">claude-code</a></h2>
    <p class="col-9">Claude Code: agentic coding tool</p>
  </article>
  <article class="Box-row">
    <h2><a href="/openai/codex">codex</a></h2>
    <p class="col-9">OpenAI Codex CLI</p>
  </article>
</body></html>
"""


@pytest.fixture
def gh_env(monkeypatch):
    repo = Path(__file__).resolve().parents[2]
    bundled = repo / "extensions" / "browser-recipes" / "recipes"
    monkeypatch.setenv("OPENCOMPUTER_RECIPES_BUNDLED_DIR", str(bundled))
    monkeypatch.setenv("OPENCOMPUTER_RECIPES_PROFILE_DIR", "/dev/null/no-profile")


def test_github_trending_recipe_loads(gh_env):
    recipe = load_recipe("github_trending")
    assert recipe.name == "github_trending"
    assert "daily" in recipe.commands
    assert "weekly" in recipe.commands
    assert "monthly" in recipe.commands


def test_github_trending_daily_runs_with_mock_html(gh_env):
    def fake_fetcher(url):
        assert "since=daily" in url
        return SAMPLE_TRENDING_HTML

    out = run_recipe(
        site="github_trending", verb="daily",
        args={"limit": 5},
        fetcher=fake_fetcher, fmt="json",
    )
    assert "claude-code" in out
    assert "codex" in out


def test_github_trending_weekly_uses_weekly_url(gh_env):
    captured = []

    def fake_fetcher(url):
        captured.append(url)
        return SAMPLE_TRENDING_HTML

    run_recipe(
        site="github_trending", verb="weekly",
        args={"limit": 2},
        fetcher=fake_fetcher, fmt="table",
    )
    assert any("since=weekly" in url for url in captured)

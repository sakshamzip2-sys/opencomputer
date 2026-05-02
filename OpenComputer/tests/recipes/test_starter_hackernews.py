"""hackernews recipe loads + runs end-to-end against a mock fetcher."""
from pathlib import Path

import pytest

from opencomputer.recipes import load_recipe, run_recipe


@pytest.fixture
def hn_env(monkeypatch):
    """Point the loader at the bundled recipes dir."""
    repo = Path(__file__).resolve().parents[2]
    bundled = repo / "extensions" / "browser-recipes" / "recipes"
    monkeypatch.setenv("OPENCOMPUTER_RECIPES_BUNDLED_DIR", str(bundled))
    monkeypatch.setenv("OPENCOMPUTER_RECIPES_PROFILE_DIR", "/dev/null/no-profile")


def test_hackernews_recipe_loads(hn_env):
    recipe = load_recipe("hackernews")
    assert recipe.name == "hackernews"
    assert "top" in recipe.commands
    assert "new" in recipe.commands
    assert "show" in recipe.commands


def test_hackernews_top_runs_with_mock_fetcher(hn_env):
    def fake_fetcher(url):
        if url.endswith("topstories.json"):
            return [42, 43, 44]
        n = int(url.split("/")[-1].replace(".json", ""))
        return {
            "id": n, "title": f"Story {n}", "url": f"https://x/{n}",
            "score": n, "by": "alice", "descendants": 0,
        }

    out = run_recipe(
        site="hackernews", verb="top", args={"limit": 2},
        fetcher=fake_fetcher, fmt="json",
    )
    assert "Story 42" in out
    assert "Story 43" in out
    assert "Story 44" not in out  # take=2


def test_hackernews_new_runs(hn_env):
    def fake_fetcher(url):
        if url.endswith("newstories.json"):
            return [1, 2]
        n = int(url.split("/")[-1].replace(".json", ""))
        return {"id": n, "title": f"New {n}", "url": "x", "score": n, "by": "z"}

    out = run_recipe(
        site="hackernews", verb="new", args={"limit": 2},
        fetcher=fake_fetcher, fmt="table",
    )
    assert "New 1" in out
    assert "New 2" in out


def test_hackernews_show_runs(hn_env):
    def fake_fetcher(url):
        if url.endswith("showstories.json"):
            return [100]
        return {"id": 100, "title": "Show story", "url": "x", "score": 5, "by": "y"}

    out = run_recipe(
        site="hackernews", verb="show", args={"limit": 1},
        fetcher=fake_fetcher, fmt="md",
    )
    assert "Show story" in out

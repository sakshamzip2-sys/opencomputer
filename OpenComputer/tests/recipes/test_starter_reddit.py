"""reddit recipe: end-to-end with mock fetcher (verifies select-step shape)."""
from pathlib import Path

import pytest

from opencomputer.recipes import load_recipe, run_recipe


@pytest.fixture
def reddit_env(monkeypatch):
    repo = Path(__file__).resolve().parents[2]
    bundled = repo / "extensions" / "browser-recipes" / "recipes"
    monkeypatch.setenv("OPENCOMPUTER_RECIPES_BUNDLED_DIR", str(bundled))
    monkeypatch.setenv("OPENCOMPUTER_RECIPES_PROFILE_DIR", "/dev/null/no-profile")


def test_reddit_recipe_loads(reddit_env):
    recipe = load_recipe("reddit")
    assert recipe.name == "reddit"
    assert "hot" in recipe.commands
    assert "new" in recipe.commands
    assert "top" in recipe.commands


def test_reddit_hot_runs_with_mock_fetcher(reddit_env):
    """hot uses select to extract data.children[*].data, then format."""
    def fake_fetcher(url):
        # Reddit shape: {"data": {"children": [{"data": {<post fields>}}, ...]}}
        return {
            "data": {
                "children": [
                    {"data": {
                        "title": f"Post {i}",
                        "url": f"https://r/p/{i}",
                        "score": 100 + i,
                        "num_comments": i,
                        "author": "alice",
                        "subreddit": "programming",
                    }}
                    for i in range(5)
                ],
            },
        }

    out = run_recipe(
        site="reddit", verb="hot",
        args={"subreddit": "programming", "limit": 5},
        fetcher=fake_fetcher, fmt="json",
    )
    assert "Post 0" in out
    assert "Post 4" in out


def test_reddit_top_uses_subreddit_arg(reddit_env):
    captured = []

    def fake_fetcher(url):
        captured.append(url)
        return {"data": {"children": []}}

    run_recipe(
        site="reddit", verb="top",
        args={"subreddit": "rust", "limit": 3},
        fetcher=fake_fetcher, fmt="json",
    )
    # The subreddit is templated into the URL.
    assert any("/r/rust/" in url for url in captured)

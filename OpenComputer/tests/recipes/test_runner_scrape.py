"""v2 scrape step: BS4-based HTML scraping (CSS selectors)."""
from unittest.mock import MagicMock

import pytest

from opencomputer.recipes.runner import run_pipeline
from opencomputer.recipes.schema import validate_recipe


def _build_recipe(pipeline):
    return validate_recipe({
        "name": "test",
        "commands": {"go": {"pipeline": pipeline}},
    }).commands["go"]


SAMPLE_HTML = """
<html><body>
  <article class="Box-row">
    <h2><a href="/foo/bar">First Repo</a></h2>
    <span class="stars">120</span>
  </article>
  <article class="Box-row">
    <h2><a href="/baz/qux">Second Repo</a></h2>
    <span class="stars">45</span>
  </article>
</body></html>
"""


def test_scrape_extracts_text_and_href():
    fetcher = MagicMock(return_value=SAMPLE_HTML)
    cmd = _build_recipe([
        {"fetch": "https://example.com/trending"},
        {"scrape": {
            "item": "article.Box-row",
            "fields": {
                "title": "h2 a",
                "href": "h2 a@href",
            },
        }},
    ])
    result = run_pipeline(cmd, args={}, fetcher=fetcher)
    assert result == [
        {"title": "First Repo", "href": "/foo/bar"},
        {"title": "Second Repo", "href": "/baz/qux"},
    ]


def test_scrape_with_extra_fields():
    fetcher = MagicMock(return_value=SAMPLE_HTML)
    cmd = _build_recipe([
        {"fetch": "https://example.com/trending"},
        {"scrape": {
            "item": "article.Box-row",
            "fields": {
                "title": "h2 a",
                "stars": "span.stars",
            },
        }},
    ])
    result = run_pipeline(cmd, args={}, fetcher=fetcher)
    assert result == [
        {"title": "First Repo", "stars": "120"},
        {"title": "Second Repo", "stars": "45"},
    ]


def test_scrape_missing_field_yields_empty_string():
    """If a field's selector doesn't match anything inside an item, return ''."""
    fetcher = MagicMock(return_value=SAMPLE_HTML)
    cmd = _build_recipe([
        {"fetch": "https://example.com/trending"},
        {"scrape": {
            "item": "article.Box-row",
            "fields": {
                "title": "h2 a",
                "missing": "div.does-not-exist",
            },
        }},
    ])
    result = run_pipeline(cmd, args={}, fetcher=fetcher)
    assert result[0]["missing"] == ""
    assert result[0]["title"] == "First Repo"


def test_scrape_no_items_returns_empty_list():
    """No items match → []."""
    fetcher = MagicMock(return_value=SAMPLE_HTML)
    cmd = _build_recipe([
        {"fetch": "https://example.com/trending"},
        {"scrape": {
            "item": "div.does-not-exist",
            "fields": {"title": "a"},
        }},
    ])
    result = run_pipeline(cmd, args={}, fetcher=fetcher)
    assert result == []


def test_scrape_then_take():
    fetcher = MagicMock(return_value=SAMPLE_HTML)
    cmd = _build_recipe([
        {"fetch": "https://example.com/trending"},
        {"scrape": {
            "item": "article.Box-row",
            "fields": {"title": "h2 a"},
        }},
        {"take": 1},
    ])
    result = run_pipeline(cmd, args={}, fetcher=fetcher)
    assert result == [{"title": "First Repo"}]

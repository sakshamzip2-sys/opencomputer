"""v2 grammar extensions: select step (JSON path) + Playwright fetcher path."""
from unittest.mock import MagicMock

import pytest

from opencomputer.recipes.runner import run_pipeline
from opencomputer.recipes.schema import validate_recipe


def _build_recipe(pipeline):
    return validate_recipe({
        "name": "test",
        "commands": {"go": {"pipeline": pipeline}},
    }).commands["go"]


def test_select_extracts_dotted_key():
    """select: 'data.children' extracts a nested list."""
    fetcher = MagicMock(return_value={
        "data": {"children": [{"x": 1}, {"x": 2}]},
    })
    cmd = _build_recipe([
        {"fetch": "https://example.com/x"},
        {"select": "data.children"},
    ])
    result = run_pipeline(cmd, args={}, fetcher=fetcher)
    assert result == [{"x": 1}, {"x": 2}]


def test_select_with_star_flattens_each_item():
    """select: 'data.children[*].data' takes inner data of each child."""
    fetcher = MagicMock(return_value={
        "data": {
            "children": [
                {"data": {"title": "first", "score": 100}},
                {"data": {"title": "second", "score": 50}},
            ],
        },
    })
    cmd = _build_recipe([
        {"fetch": "https://example.com/x"},
        {"select": "data.children[*].data"},
    ])
    result = run_pipeline(cmd, args={}, fetcher=fetcher)
    assert result == [
        {"title": "first", "score": 100},
        {"title": "second", "score": 50},
    ]


def test_select_then_format():
    """select extracts, format projects fields."""
    fetcher = MagicMock(return_value={
        "data": {"children": [
            {"data": {"title": "a", "score": 10, "extra": "x"}},
            {"data": {"title": "b", "score": 20, "extra": "y"}},
        ]},
    })
    cmd = _build_recipe([
        {"fetch": "https://example.com/x"},
        {"select": "data.children[*].data"},
        {"format": {"fields": ["title", "score"]}},
    ])
    result = run_pipeline(cmd, args={}, fetcher=fetcher)
    assert result == [
        {"title": "a", "score": 10},
        {"title": "b", "score": 20},
    ]


def test_select_missing_path_returns_empty():
    """A path that doesn't exist returns []  rather than raising."""
    fetcher = MagicMock(return_value={"data": "not a dict with children"})
    cmd = _build_recipe([
        {"fetch": "https://example.com/x"},
        {"select": "data.children"},
    ])
    result = run_pipeline(cmd, args={}, fetcher=fetcher)
    assert result == []


def test_select_then_take_then_format():
    """Full reddit-shaped pipeline: select, take, format."""
    fetcher = MagicMock(return_value={
        "data": {"children": [
            {"data": {"title": f"post {i}", "url": f"u{i}", "score": i}}
            for i in range(10)
        ]},
    })
    cmd = _build_recipe([
        {"fetch": "https://example.com/x"},
        {"select": "data.children[*].data"},
        {"take": 3},
        {"format": {"fields": ["title", "score"]}},
    ])
    result = run_pipeline(cmd, args={}, fetcher=fetcher)
    assert result == [
        {"title": "post 0", "score": 0},
        {"title": "post 1", "score": 1},
        {"title": "post 2", "score": 2},
    ]

"""Pipeline runner — executes recipe pipelines against a fetcher mock."""
from unittest.mock import MagicMock

import pytest

from opencomputer.recipes.runner import run_pipeline
from opencomputer.recipes.schema import validate_recipe


def _build_recipe(pipeline):
    return validate_recipe({
        "name": "test",
        "commands": {"go": {"pipeline": pipeline}},
    }).commands["go"]


def test_pipeline_with_static_take():
    fake_fetcher = MagicMock(return_value=[1, 2, 3, 4, 5])
    cmd = _build_recipe([
        {"fetch": "https://example.com/list.json"},
        {"take": 3},
    ])
    result = run_pipeline(cmd, args={}, fetcher=fake_fetcher)
    assert result == [1, 2, 3]


def test_pipeline_with_templated_take():
    fake_fetcher = MagicMock(return_value=list(range(20)))
    cmd = _build_recipe([
        {"fetch": "https://example.com/list.json"},
        {"take": "{{ limit }}"},
    ])
    result = run_pipeline(cmd, args={"limit": 5}, fetcher=fake_fetcher)
    assert result == [0, 1, 2, 3, 4]


def test_pipeline_with_default_template():
    """{{ limit | default(10) }} works when args lacks 'limit'."""
    fake_fetcher = MagicMock(return_value=list(range(20)))
    cmd = _build_recipe([
        {"fetch": "https://example.com/list.json"},
        {"take": "{{ limit | default(10) }}"},
    ])
    result = run_pipeline(cmd, args={}, fetcher=fake_fetcher)
    assert result == list(range(10))


def test_pipeline_map_then_format():
    def fetch(url):
        if "list" in url:
            return [1, 2]
        n = int(url.split("/")[-1].replace(".json", ""))
        return {"id": n, "title": f"item {n}", "extra": "ignored"}

    cmd = _build_recipe([
        {"fetch": "https://example.com/list.json"},
        {"map": {"fetch": "https://example.com/item/{{ item }}.json"}},
        {"format": {"fields": ["id", "title"]}},
    ])
    result = run_pipeline(cmd, args={}, fetcher=fetch)
    assert result == [
        {"id": 1, "title": "item 1"},
        {"id": 2, "title": "item 2"},
    ]


def test_pipeline_filter_keeps_truthy():
    fetcher = MagicMock(return_value=[
        {"score": 100}, {"score": 50}, {"score": 200},
    ])
    cmd = _build_recipe([
        {"fetch": "https://example.com/list.json"},
        {"filter": "{{ item.score >= 100 }}"},
    ])
    result = run_pipeline(cmd, args={}, fetcher=fetcher)
    assert result == [{"score": 100}, {"score": 200}]


def test_pipeline_take_on_non_list_raises():
    fetcher = MagicMock(return_value="not a list")
    cmd = _build_recipe([
        {"fetch": "https://example.com/x"},
        {"take": 3},
    ])
    with pytest.raises(TypeError):
        run_pipeline(cmd, args={}, fetcher=fetcher)

"""Recipe schema validation."""
import pytest
import yaml

from opencomputer.recipes.schema import validate_recipe


VALID_HN_YAML = """
name: hackernews
description: Hacker News scrapers
commands:
  top:
    description: Top stories from HN
    pipeline:
      - fetch: https://hacker-news.firebaseio.com/v0/topstories.json
      - take: "{{ limit | default(10) }}"
      - map:
          fetch: https://hacker-news.firebaseio.com/v0/item/{{ item }}.json
      - format:
          fields: [title, url, score, by]
    formats: [json, table, md]
"""


def test_valid_recipe_loads():
    data = yaml.safe_load(VALID_HN_YAML)
    recipe = validate_recipe(data)
    assert recipe.name == "hackernews"
    assert "top" in recipe.commands
    assert recipe.commands["top"].pipeline


def test_recipe_requires_name():
    bad = {"description": "x", "commands": {}}
    with pytest.raises(Exception):
        validate_recipe(bad)


def test_command_requires_pipeline():
    bad = {
        "name": "foo",
        "commands": {"hot": {"description": "x"}},
    }
    with pytest.raises(Exception):
        validate_recipe(bad)


def test_pipeline_steps_must_be_known_kinds():
    bad = {
        "name": "foo",
        "commands": {
            "hot": {
                "pipeline": [{"unknown_step": "value"}],
            },
        },
    }
    with pytest.raises(Exception):
        validate_recipe(bad)

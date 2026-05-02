"""Pydantic models for recipe YAML.

A recipe is a per-site scraping spec. Each command is a named verb
(e.g. 'top', 'hot', 'bookmarks') with a pipeline of steps.

Pipeline step kinds (v1):
  - fetch: <url>            HTTP GET, parse JSON if Content-Type matches
  - take: <int|template>    Slice the iterable to N items
  - map: <step>             Apply <step> to each item; replaces item with result
  - filter: <jinja-expr>    Keep items where the expression is truthy
  - format:                 Pick fields and shape output
      fields: [title, url]
  - eval: <jinja-expr>      Run a jinja expression on the current value

Templates use simple jinja2: {{ item }}, {{ limit | default(10) }}, etc.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

PipelineStepKind = Literal[
    "fetch", "take", "map", "filter", "format", "eval", "select", "scrape"
]
KNOWN_KINDS: set[str] = {
    "fetch", "take", "map", "filter", "format", "eval", "select", "scrape",
}


class Command(BaseModel):
    """One verb on a site (e.g. 'top', 'hot', 'bookmarks')."""

    description: str = ""
    pipeline: list[dict[str, Any]] = Field(min_length=1)
    formats: list[Literal["json", "table", "md", "csv"]] = ["json"]

    @field_validator("pipeline")
    @classmethod
    def _validate_steps(cls, v: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for i, step in enumerate(v):
            if not isinstance(step, dict) or len(step) != 1:
                raise ValueError(
                    f"step {i} must be a dict with exactly one key (the kind)"
                )
            (kind,) = step.keys()
            if kind not in KNOWN_KINDS:
                raise ValueError(
                    f"step {i} kind {kind!r} not in known kinds {sorted(KNOWN_KINDS)}"
                )
        return v


class Recipe(BaseModel):
    """A site's recipe — name, description, and a dict of named commands."""

    name: str
    description: str = ""
    commands: dict[str, Command]


def validate_recipe(data: dict[str, Any]) -> Recipe:
    """Construct a Recipe from a raw dict (e.g. yaml.safe_load output).

    Raises pydantic.ValidationError on malformed data.
    """
    return Recipe.model_validate(data)

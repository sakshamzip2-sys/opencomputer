"""Pipeline executor.

Runs a recipe Command's pipeline against a fetcher (a callable
``fetch(url) -> dict | list``). The default fetcher uses httpx; tests
inject a mock.

Templates (jinja2-shaped, simple syntax):
  {{ item }}                  current value in a map
  {{ limit | default(10) }}   args["limit"] or 10
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from jinja2 import Environment, StrictUndefined

from opencomputer.recipes.schema import Command


def _render(template: Any, ctx: dict[str, Any]) -> Any:
    """Render a template string against ctx; pass-through non-strings."""
    if not isinstance(template, str):
        return template
    if "{{" not in template and "{%" not in template:
        return template
    env = Environment(undefined=StrictUndefined, autoescape=False)
    return env.from_string(template).render(**ctx)


def _coerce_int(s: Any) -> int:
    if isinstance(s, int):
        return s
    return int(str(s))


def _eval_truthy(template: str, ctx: dict[str, Any]) -> bool:
    rendered = _render(template, ctx)
    if isinstance(rendered, str):
        rendered = rendered.strip().lower()
        return rendered not in ("", "false", "0", "none")
    return bool(rendered)


def run_pipeline(
    cmd: Command,
    *,
    args: dict[str, Any],
    fetcher: Callable[[str], Any],
) -> Any:
    """Execute a recipe command's pipeline; return final value.

    ``fetcher`` is the URL -> JSON-or-list-of-dicts callable. Tests inject
    a mock; production wires in the httpx default fetcher.
    """
    value: Any = None
    for step in cmd.pipeline:
        ((kind, spec),) = step.items()
        ctx: dict[str, Any] = {**args, "value": value}
        if kind == "fetch":
            url = _render(spec, ctx)
            value = fetcher(url)
        elif kind == "take":
            n = _coerce_int(_render(spec, ctx))
            if not isinstance(value, list):
                raise TypeError(f"take requires list, got {type(value).__name__}")
            value = value[:n]
        elif kind == "map":
            inner_step = spec  # dict like {"fetch": "..."}
            ((inner_kind, inner_spec),) = inner_step.items()
            if inner_kind != "fetch":
                raise NotImplementedError(
                    f"map currently only supports inner kind 'fetch', got {inner_kind!r}"
                )
            if not isinstance(value, list):
                raise TypeError(f"map requires list, got {type(value).__name__}")
            mapped = []
            for item in value:
                item_ctx = {**args, "item": item}
                url = _render(inner_spec, item_ctx)
                mapped.append(fetcher(url))
            value = mapped
        elif kind == "filter":
            if not isinstance(value, list):
                raise TypeError(f"filter requires list, got {type(value).__name__}")
            value = [
                item for item in value
                if _eval_truthy(spec, {**args, "item": item})
            ]
        elif kind == "format":
            fields = (spec or {}).get("fields") or []
            if not isinstance(value, list):
                raise TypeError(f"format requires list, got {type(value).__name__}")
            value = [
                {f: item.get(f) for f in fields} for item in value
                if isinstance(item, dict)
            ]
        elif kind == "eval":
            value = _render(spec, ctx)
        else:
            raise ValueError(f"unknown pipeline step kind: {kind}")
    return value

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


def _scrape_html(html: Any, spec: dict[str, Any]) -> list[dict[str, str]]:
    """BS4-based HTML scraper.

    spec shape:
      item: "<css selector>"           # iterates these
      fields:
        <name>: "<css selector>"       # text of matched element
        <name>: "<css selector>@<attr>" # value of attribute
    """
    if not isinstance(html, str):
        return []

    from bs4 import BeautifulSoup

    item_selector = spec.get("item", "")
    fields: dict[str, str] = spec.get("fields") or {}
    if not item_selector or not fields:
        return []

    soup = BeautifulSoup(html, "html.parser")
    items = soup.select(item_selector)
    out: list[dict[str, str]] = []
    for item in items:
        row: dict[str, str] = {}
        for name, sel in fields.items():
            sel_str = str(sel)
            attr: str | None = None
            if "@" in sel_str:
                sel_str, _, attr = sel_str.partition("@")
                sel_str = sel_str.strip()
                attr = attr.strip() or None
            elem = item.select_one(sel_str) if sel_str else item
            if elem is None:
                row[name] = ""
                continue
            if attr:
                row[name] = elem.get(attr, "") or ""
            else:
                row[name] = elem.get_text(strip=True)
        out.append(row)
    return out


def _select_path(value: Any, path: str) -> Any:
    """Walk a dotted JSON path with optional [*] flatten markers.

    Examples:
      'data.children'         -> value['data']['children']
      'data.children[*].data' -> [c['data'] for c in value['data']['children']]
      'a.b.c'                 -> value['a']['b']['c']

    Missing keys / wrong types yield [] rather than raising — recipes are
    "best effort" by design; the user gets an empty result and can
    refine the path.
    """
    parts: list[str] = []
    for piece in path.split("."):
        # Split each piece on [*] to track the flatten step.
        if "[*]" in piece:
            head, _, rest = piece.partition("[*]")
            if head:
                parts.append(head)
            parts.append("[*]")
            if rest:
                # Anything after [*] in the same dotted segment is invalid;
                # recipes should put the next key after a fresh dot.
                # We treat it as a normal sub-key for forgiveness.
                parts.append(rest.lstrip("."))
        else:
            parts.append(piece)

    current = value
    for part in parts:
        if part == "[*]":
            if not isinstance(current, list):
                return []
            # Flatten: continue with each element; if there are more parts
            # after [*], they apply to each element below.
            tail = parts[parts.index("[*]") + 1:]
            if not tail:
                return current
            results = []
            for item in current:
                # Recursively select the tail on each item.
                sub = _select_path(item, ".".join(tail))
                if isinstance(sub, list):
                    results.extend(sub)
                else:
                    results.append(sub)
            return results
        if not isinstance(current, dict):
            return []
        current = current.get(part)
        if current is None:
            return []
    return current


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
        elif kind == "select":
            path = str(_render(spec, ctx))
            value = _select_path(value, path)
        elif kind == "scrape":
            value = _scrape_html(value, spec or {})
        else:
            raise ValueError(f"unknown pipeline step kind: {kind}")
    return value

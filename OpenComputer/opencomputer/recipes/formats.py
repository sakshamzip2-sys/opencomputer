"""Output formatting for recipe results."""

from __future__ import annotations

import json
from typing import Any, Literal

Fmt = Literal["json", "table", "md", "csv"]


def format_output(rows: list[dict[str, Any]] | Any, *, fmt: Fmt = "json") -> str:
    """Render a list of dicts (or any value) as ``fmt``."""
    if fmt == "json":
        return json.dumps(rows, indent=2, default=str)

    if not isinstance(rows, list):
        rows = [rows] if rows is not None else []

    if not rows:
        if fmt == "table" or fmt == "md":
            return "(no rows)\n"
        if fmt == "csv":
            return ""

    if fmt == "table":
        return _format_table(rows)
    if fmt == "md":
        return _format_md(rows)
    if fmt == "csv":
        return _format_csv(rows)
    raise ValueError(f"unknown format {fmt!r}; use one of: json, table, md, csv")


def _all_keys(rows: list[dict[str, Any]]) -> list[str]:
    seen: list[str] = []
    seen_set: set[str] = set()
    for r in rows:
        if isinstance(r, dict):
            for k in r:
                if k not in seen_set:
                    seen_set.add(k)
                    seen.append(k)
    return seen


def _format_table(rows: list[dict[str, Any]]) -> str:
    keys = _all_keys(rows)
    widths = {
        k: max(
            len(k),
            max((len(str(r.get(k, ""))) for r in rows), default=0),
        )
        for k in keys
    }
    header = "  ".join(k.ljust(widths[k]) for k in keys)
    sep = "  ".join("-" * widths[k] for k in keys)
    lines = [header, sep]
    for r in rows:
        lines.append("  ".join(str(r.get(k, "")).ljust(widths[k]) for k in keys))
    return "\n".join(lines) + "\n"


def _format_md(rows: list[dict[str, Any]]) -> str:
    keys = _all_keys(rows)
    header = "| " + " | ".join(keys) + " |"
    sep = "| " + " | ".join("---" for _ in keys) + " |"
    body = [
        "| " + " | ".join(str(r.get(k, "")) for k in keys) + " |"
        for r in rows
    ]
    return "\n".join([header, sep, *body]) + "\n"


def _format_csv(rows: list[dict[str, Any]]) -> str:
    import csv
    import io

    keys = _all_keys(rows)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=keys, extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()

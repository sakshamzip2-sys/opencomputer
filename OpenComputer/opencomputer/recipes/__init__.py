"""OpenCLI-style recipe layer for browser scraping commands.

Public API:
    load_recipe(site)       -> Recipe
    run_recipe(site, verb, *, args, fetcher, fmt) -> str
    list_recipes()          -> list[str]
"""

from typing import Any, Callable

from opencomputer.recipes.formats import Fmt, format_output
from opencomputer.recipes.loader import list_recipes, load_recipe
from opencomputer.recipes.runner import run_pipeline


def run_recipe(
    *,
    site: str,
    verb: str,
    args: dict[str, Any],
    fetcher: Callable[[str], Any],
    fmt: Fmt = "json",
) -> str:
    """Load + run + format. Raises KeyError for unknown site or unknown verb."""
    recipe = load_recipe(site)
    if verb not in recipe.commands:
        raise KeyError(
            f"site {site!r} has no verb {verb!r}. "
            f"Known: {sorted(recipe.commands)}"
        )
    cmd = recipe.commands[verb]
    rows = run_pipeline(cmd, args=args, fetcher=fetcher)
    return format_output(rows, fmt=fmt)


__all__ = ["list_recipes", "load_recipe", "run_recipe"]

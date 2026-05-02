"""Recipe file discovery + parsing.

Search order (highest priority first):
  1. OPENCOMPUTER_RECIPES_PROFILE_DIR  (default: ~/.opencomputer/<profile>/recipes/)
  2. OPENCOMPUTER_RECIPES_BUNDLED_DIR  (default: <repo>/extensions/browser-recipes/recipes/)

Each file is a single recipe ('site_name.yaml'). Filename stem is the site key.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from opencomputer.recipes.schema import Recipe, validate_recipe


def _profile_dir() -> Path:
    env = os.environ.get("OPENCOMPUTER_RECIPES_PROFILE_DIR")
    if env:
        return Path(env)
    home = (
        Path.home()
        / ".opencomputer"
        / os.environ.get("OPENCOMPUTER_PROFILE", "default")
    )
    return home / "recipes"


def _bundled_dir() -> Path:
    env = os.environ.get("OPENCOMPUTER_RECIPES_BUNDLED_DIR")
    if env:
        return Path(env)
    repo = Path(__file__).resolve().parents[2]  # opencomputer/recipes/loader.py -> repo
    return repo / "extensions" / "browser-recipes" / "recipes"


def _candidate_paths(site: str) -> list[Path]:
    """Profile-local first, then bundled."""
    return [
        _profile_dir() / f"{site}.yaml",
        _bundled_dir() / f"{site}.yaml",
    ]


def load_recipe(site: str) -> Recipe:
    """Find and parse the recipe for ``site``.

    Profile-local recipes override bundled ones. Raises KeyError if no
    recipe file is found in either dir.
    """
    for path in _candidate_paths(site):
        if path.exists():
            data = yaml.safe_load(path.read_text())
            return validate_recipe(data)
    raise KeyError(
        f"No recipe for site {site!r}. Searched: "
        + ", ".join(str(p) for p in _candidate_paths(site))
    )


def list_recipes() -> list[str]:
    """Return all recipe site names available in profile + bundled dirs.

    Profile-local names override bundled ones (set semantics, dedup'd).
    """
    seen: set[str] = set()
    for d in (_profile_dir(), _bundled_dir()):
        if d.exists():
            for f in d.glob("*.yaml"):
                seen.add(f.stem)
    return sorted(seen)

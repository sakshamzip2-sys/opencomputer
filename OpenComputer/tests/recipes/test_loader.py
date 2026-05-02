"""Loader: profile-local recipes override bundled ones; missing site raises."""
from pathlib import Path

import pytest
import yaml

from opencomputer.recipes.loader import list_recipes, load_recipe


def _write_recipe(path: Path, name: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({
        "name": name,
        "commands": {
            "ping": {
                "pipeline": [{"fetch": "https://example.com/ping"}],
            },
        },
    }))


def test_load_from_bundled(tmp_path, monkeypatch):
    bundled = tmp_path / "bundled"
    profile = tmp_path / "profile"
    _write_recipe(bundled / "site_a.yaml", "site_a")

    monkeypatch.setenv("OPENCOMPUTER_RECIPES_BUNDLED_DIR", str(bundled))
    monkeypatch.setenv("OPENCOMPUTER_RECIPES_PROFILE_DIR", str(profile))

    recipe = load_recipe("site_a")
    assert recipe.name == "site_a"


def test_profile_overrides_bundled(tmp_path, monkeypatch):
    bundled = tmp_path / "bundled"
    profile = tmp_path / "profile"
    _write_recipe(bundled / "site_a.yaml", "site_a_bundled")
    _write_recipe(profile / "site_a.yaml", "site_a_profile")

    monkeypatch.setenv("OPENCOMPUTER_RECIPES_BUNDLED_DIR", str(bundled))
    monkeypatch.setenv("OPENCOMPUTER_RECIPES_PROFILE_DIR", str(profile))

    recipe = load_recipe("site_a")
    assert recipe.name == "site_a_profile"  # profile wins


def test_unknown_site_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_RECIPES_BUNDLED_DIR", str(tmp_path / "bundled"))
    monkeypatch.setenv("OPENCOMPUTER_RECIPES_PROFILE_DIR", str(tmp_path / "profile"))

    with pytest.raises(KeyError):
        load_recipe("does_not_exist")


def test_list_recipes_combines_dirs(tmp_path, monkeypatch):
    bundled = tmp_path / "bundled"
    profile = tmp_path / "profile"
    _write_recipe(bundled / "alpha.yaml", "alpha")
    _write_recipe(profile / "beta.yaml", "beta")

    monkeypatch.setenv("OPENCOMPUTER_RECIPES_BUNDLED_DIR", str(bundled))
    monkeypatch.setenv("OPENCOMPUTER_RECIPES_PROFILE_DIR", str(profile))

    names = sorted(list_recipes())
    assert names == ["alpha", "beta"]

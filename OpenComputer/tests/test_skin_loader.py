"""SkinSpec dataclass shape + 9 built-in YAML loadability."""
from __future__ import annotations

import pytest

from opencomputer.cli_ui.skin import SkinSpec, list_builtin_names, load_skin


def test_skinspec_is_frozen():
    spec = SkinSpec(
        name="test",
        description="x",
        colors={"banner_border": "#FFFFFF"},
        spinner_thinking_verbs=("thinking",),
        spinner_wings=(("⟨", "⟩"),),
        agent_name="Test",
        response_label=" Test ",
        prompt_symbol=">",
        banner_logo="",
        banner_hero="",
        tool_prefix="┊",
        tool_emojis={},
    )
    with pytest.raises(Exception):
        spec.name = "no"  # type: ignore[misc]


def test_all_9_builtins_listed():
    names = list_builtin_names()
    expected = {
        "default", "ares", "mono", "slate", "daylight",
        "warm-lightmode", "poseidon", "sisyphus", "charizard",
    }
    assert expected.issubset(set(names))


def test_all_9_builtins_load():
    for name in list_builtin_names():
        spec = load_skin(name)
        assert spec.name == name
        assert spec.colors  # at least one color set
        # branding fields are non-empty after default-merge
        assert spec.agent_name


def test_unknown_skin_falls_back_to_default():
    spec = load_skin("nonexistent_xyz_blah")
    assert spec.name == "default"


def test_default_skin_loads_clean():
    spec = load_skin("default")
    assert spec.name == "default"
    assert "banner_border" in spec.colors


def test_user_skin_overrides_builtin(tmp_path, monkeypatch):
    """A skin file under USER_SKINS_DIR overrides the built-in name."""
    from opencomputer.cli_ui.skin import loader as _loader

    user_dir = tmp_path / "skins"
    user_dir.mkdir()
    (user_dir / "ares.yaml").write_text(
        "name: ares\n"
        "description: my custom ares\n"
        "colors:\n"
        "  agent_text: '#123456'\n"
    )
    monkeypatch.setattr(_loader, "USER_SKINS_DIR", user_dir)

    spec = load_skin("ares")
    assert spec.name == "ares"
    assert spec.colors["agent_text"] == "#123456"


def test_malformed_yaml_falls_back(tmp_path, monkeypatch):
    """Invalid YAML for a skin doesn't crash; falls back to default."""
    from opencomputer.cli_ui.skin import loader as _loader

    user_dir = tmp_path / "skins"
    user_dir.mkdir()
    (user_dir / "broken.yaml").write_text("this is :: not yaml:\n  - [unclosed\n")
    monkeypatch.setattr(_loader, "USER_SKINS_DIR", user_dir)

    spec = load_skin("broken")
    # malformed override → falls back to default-only data
    assert spec.colors  # default colors still present


def test_missing_keys_inherit_from_default(tmp_path, monkeypatch):
    """A custom skin specifying only one color inherits other defaults."""
    from opencomputer.cli_ui.skin import loader as _loader

    user_dir = tmp_path / "skins"
    user_dir.mkdir()
    (user_dir / "minimal.yaml").write_text(
        "colors:\n"
        "  agent_text: '#ABCDEF'\n"
    )
    monkeypatch.setattr(_loader, "USER_SKINS_DIR", user_dir)

    spec = load_skin("minimal")
    assert spec.colors["agent_text"] == "#ABCDEF"
    # default skin's banner_border should be inherited
    assert "banner_border" in spec.colors
    assert spec.colors["banner_border"] != ""

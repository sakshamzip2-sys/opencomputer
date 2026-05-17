"""Personality loader: builtins, custom override, resolution chain."""
from __future__ import annotations

import pytest

from opencomputer.agent.personality import Personality, resolve


def test_builtin_helpful_resolves_to_nonempty_body():
    p = resolve("helpful", custom={})
    assert isinstance(p, Personality)
    assert p.name == "helpful"
    assert len(p.body) > 50
    assert "helpful" in p.body.lower() or "user" in p.body.lower()


def test_all_builtins_resolve():
    # Iterate BUILTINS directly so this never goes stale when a register
    # is added (e.g. explanatory / learning — best-of-three R9).
    from opencomputer.agent.personality import BUILTINS

    assert {"explanatory", "learning"} <= set(BUILTINS)
    for name in BUILTINS:
        p = resolve(name, custom={})
        assert p.name == name
        assert p.body.strip(), f"{name} has empty body"


def test_unknown_name_falls_back_to_helpful():
    p = resolve("nonexistent_xyz", custom={})
    assert p.name == "helpful"


def test_empty_name_returns_helpful():
    p = resolve("", custom={})
    assert p.name == "helpful"


def test_custom_overrides_builtin():
    custom = {"helpful": "OVERRIDE BODY"}
    p = resolve("helpful", custom=custom)
    assert p.name == "helpful"
    assert p.body == "OVERRIDE BODY"


def test_custom_only_name():
    custom = {"codereviewer": "Be thorough about bugs."}
    p = resolve("codereviewer", custom=custom)
    assert p.name == "codereviewer"
    assert p.body == "Be thorough about bugs."


def test_malformed_custom_entry_skipped():
    custom = {"good": "OK", "bad": None}  # type: ignore[dict-item]
    p_good = resolve("good", custom=custom)
    assert p_good.body == "OK"
    p_bad = resolve("bad", custom=custom)
    assert p_bad.name == "helpful"  # falls back


def test_personality_dataclass_is_frozen():
    p = resolve("helpful", custom={})
    with pytest.raises(Exception):
        p.body = "no"  # type: ignore[misc]


def test_set_default_personality_persists(tmp_path):
    """``set_default_personality`` writes to agent.default_personality."""
    from opencomputer.agent.profile_yaml import (
        load_yaml,
        set_default_personality,
    )

    cfg = tmp_path / "config.yaml"

    set_default_personality(cfg, "concise")
    data = load_yaml(cfg)
    assert data["agent"]["default_personality"] == "concise"

    set_default_personality(cfg, "")
    data = load_yaml(cfg)
    # empty string clears the key
    assert "default_personality" not in (data.get("agent") or {})


def test_set_display_skin_persists(tmp_path):
    """``set_display_skin`` writes to display.skin."""
    from opencomputer.agent.profile_yaml import (
        load_yaml,
        set_display_skin,
    )

    cfg = tmp_path / "config.yaml"

    set_display_skin(cfg, "ares")
    data = load_yaml(cfg)
    assert data["display"]["skin"] == "ares"

    set_display_skin(cfg, "")
    data = load_yaml(cfg)
    assert "skin" not in (data.get("display") or {})


def test_personality_threads_through_promptbuilder_factory(tmp_path):
    """A custom personality declared in config reaches the prompt."""
    from opencomputer.agent.profile_yaml import load_yaml
    from opencomputer.agent.prompt_builder import PromptBuilder

    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "agent:\n"
        "  personalities:\n"
        "    codereviewer: |\n"
        "      MARKER-CODE-REVIEW-XYZ\n"
    )
    loaded = load_yaml(cfg)
    custom = (loaded.get("agent") or {}).get("personalities") or {}
    assert "codereviewer" in custom

    pb = PromptBuilder()
    prompt = pb.build(personality="codereviewer", custom_personalities=custom)
    assert "MARKER-CODE-REVIEW-XYZ" in prompt

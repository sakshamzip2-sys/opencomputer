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


def test_all_14_builtins_resolve():
    expected = {
        "helpful", "concise", "technical", "creative", "teacher",
        "kawaii", "catgirl", "pirate", "shakespeare", "surfer",
        "noir", "uwu", "philosopher", "hype",
    }
    for name in expected:
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

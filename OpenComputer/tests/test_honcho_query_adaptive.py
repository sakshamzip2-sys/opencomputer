"""T4 — Honcho query-adaptive dialectic reasoning level.

Hermes-doc heuristic: bump the reasoning level by 1 step at ≥120 chars
of user query, by 2 steps at ≥400 chars, capped at ``reasoning_level_cap``.

The provider lives at ``extensions/memory-honcho/`` (hyphenated, not a
Python package), so we load it by file path the same way the existing
Honcho tests do.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_provider_mod():
    if "memory_honcho_provider_test" in sys.modules:
        return sys.modules["memory_honcho_provider_test"]
    spec_path = (
        Path(__file__).parent.parent
        / "extensions"
        / "memory-honcho"
        / "provider.py"
    )
    spec = importlib.util.spec_from_file_location(
        "memory_honcho_provider_test", spec_path
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["memory_honcho_provider_test"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_short_query_no_boost():
    mod = _load_provider_mod()
    assert mod._adapt_reasoning_level("low", "hi", "high") == "low"


def test_119_char_query_no_boost():
    mod = _load_provider_mod()
    query = "x" * 119
    assert mod._adapt_reasoning_level("low", query, "high") == "low"


def test_120_char_query_one_boost():
    mod = _load_provider_mod()
    query = "x" * 120
    assert mod._adapt_reasoning_level("low", query, "high") == "medium"


def test_399_char_query_one_boost():
    mod = _load_provider_mod()
    query = "x" * 399
    assert mod._adapt_reasoning_level("low", query, "high") == "medium"


def test_400_char_query_two_boost():
    mod = _load_provider_mod()
    query = "x" * 400
    assert mod._adapt_reasoning_level("low", query, "high") == "high"


def test_cap_clamps_boost():
    mod = _load_provider_mod()
    # base=medium + 2 boost would be "...beyond high" — clamp to cap=medium.
    query = "x" * 410
    assert mod._adapt_reasoning_level("medium", query, "medium") == "medium"


def test_unknown_base_falls_back_to_base():
    mod = _load_provider_mod()
    assert mod._adapt_reasoning_level("ludicrous", "x" * 410, "high") == "ludicrous"


def test_config_defaults():
    mod = _load_provider_mod()
    cfg = mod.HonchoConfig()
    assert cfg.dialectic_reasoning_level == "low"
    assert cfg.reasoning_level_cap == "high"


def test_config_can_be_overridden():
    mod = _load_provider_mod()
    cfg = mod.HonchoConfig(dialectic_reasoning_level="medium", reasoning_level_cap="medium")
    assert cfg.dialectic_reasoning_level == "medium"
    assert cfg.reasoning_level_cap == "medium"

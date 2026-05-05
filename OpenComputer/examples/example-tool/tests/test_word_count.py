"""Tests for the WordCount tool — drop these into your own plugin."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_tools():
    """Load example_tool/tools.py without requiring the package install."""
    name = "example_tool_tools_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name,
        Path(__file__).resolve().parent.parent / "example_tool" / "tools.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_word_count_basic():
    tools = _load_tools()
    out = tools.count("Hello world.")
    assert out.chars == 12
    assert out.words == 2
    assert out.sentences == 1


def test_word_count_multi_sentence():
    tools = _load_tools()
    out = tools.count("First. Second! Third? Fourth.")
    assert out.sentences == 4


def test_word_count_empty_string():
    tools = _load_tools()
    out = tools.count("")
    assert out.chars == 0
    assert out.words == 0
    assert out.sentences == 0


def test_word_count_only_whitespace():
    tools = _load_tools()
    out = tools.count("   \n\t  ")
    assert out.words == 0
    assert out.sentences == 0

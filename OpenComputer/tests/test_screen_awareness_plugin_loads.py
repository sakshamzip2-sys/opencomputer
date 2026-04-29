"""Smoke test: the screen-awareness plugin module imports cleanly and
exposes a register(api) entry point."""
from __future__ import annotations

from pathlib import Path


def test_plugin_module_importable():
    import importlib.util

    plugin_path = (
        Path(__file__).resolve().parent.parent
        / "extensions"
        / "screen-awareness"
        / "plugin.py"
    )
    assert plugin_path.exists(), f"plugin.py missing at {plugin_path}"
    spec = importlib.util.spec_from_file_location(
        "screen_awareness_plugin", plugin_path
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert hasattr(module, "register"), "plugin.py must expose register(api)"


def test_plugin_json_is_valid():
    import json

    plugin_json = (
        Path(__file__).resolve().parent.parent
        / "extensions"
        / "screen-awareness"
        / "plugin.json"
    )
    assert plugin_json.exists()
    data = json.loads(plugin_json.read_text(encoding="utf-8"))
    assert data.get("name") == "screen-awareness"
    assert data.get("kind") in {"sensor", "tools", "mixed"}

"""Smoke test: the screen-awareness plugin module imports cleanly and
exposes a register(api) entry point."""
from __future__ import annotations

from pathlib import Path


def test_plugin_module_importable():
    """Loadable via the package path the conftest sets up — this is
    how tests + the plugin loader actually import it. (The plugin uses
    relative imports for its sibling modules; loading via spec without
    a parent package would fail those.)"""
    plugin_path = (
        Path(__file__).resolve().parent.parent
        / "extensions"
        / "screen-awareness"
        / "plugin.py"
    )
    assert plugin_path.exists(), f"plugin.py missing at {plugin_path}"
    from extensions.screen_awareness import plugin as module

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

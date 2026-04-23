"""Smoke tests for the weather-example reference plugin.

These tests only use ``plugin_sdk`` + stdlib — they demonstrate that a
plugin is testable without importing any OpenComputer core modules.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path


def _load_plugin_module():
    plugin_root = Path(__file__).resolve().parent.parent
    # Put the plugin root on sys.path so ``from provider import ...``
    # works the same way the real loader sets it up.
    sys.path.insert(0, str(plugin_root))
    try:
        spec = importlib.util.spec_from_file_location(
            "_test_weather_example_plugin",
            plugin_root / "plugin.py",
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        try:
            sys.path.remove(str(plugin_root))
        except ValueError:
            pass


def test_plugin_register_is_callable() -> None:
    module = _load_plugin_module()
    assert callable(module.register)


def test_provider_returns_hardcoded_reply() -> None:
    """The demo provider must return DEMO_REPLY regardless of input."""
    plugin_root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(plugin_root))
    try:
        from provider import DEMO_REPLY, WeatherExampleProvider
    finally:
        sys.path.remove(str(plugin_root))

    provider = WeatherExampleProvider()
    response = asyncio.run(
        provider.complete(
            model="demo-weather-v1",
            messages=[],
            system="",
        )
    )
    assert response.message.content == DEMO_REPLY
    assert response.stop_reason == "end_turn"

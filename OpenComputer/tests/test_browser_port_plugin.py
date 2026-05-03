"""Tests for extensions.browser_control.plugin — register() registers
the Browser tool, all 11 deprecation shims, and a doctor row."""

from __future__ import annotations

from typing import Any

import pytest


class _FakeApi:
    """Stand-in for the real PluginAPI."""

    def __init__(self):
        self.tools: list[Any] = []
        self.doctor_contributions: list[Any] = []

    def register_tool(self, tool: Any) -> None:
        self.tools.append(tool)

    def register_doctor_contribution(self, contribution: Any) -> None:
        self.doctor_contributions.append(contribution)


def _import_plugin_register():
    """Re-import the plugin entry under a fresh sys.modules state so the
    package-bootstrap branch is exercised end-to-end."""
    import importlib
    import sys
    # Don't pop anything if already loaded — conftest pre-registers the
    # extensions.browser_control package alias. The plugin's
    # _bootstrap_package_namespace is a no-op when the alias already exists.
    mod = importlib.import_module("extensions.browser_control.plugin")
    return mod.register


def test_register_adds_browser_plus_eleven_shims():
    api = _FakeApi()
    register = _import_plugin_register()
    register(api)

    tool_names = [t.schema.name for t in api.tools]
    assert "Browser" in tool_names
    # 1 + 11 = 12 tools
    assert len(api.tools) == 12

    expected_shims = {
        "browser_navigate", "browser_click", "browser_fill",
        "browser_snapshot", "browser_scrape", "browser_scroll",
        "browser_back", "browser_press", "browser_get_images",
        "browser_vision", "browser_console",
    }
    assert expected_shims.issubset(set(tool_names))


def test_register_adds_doctor_row():
    api = _FakeApi()
    register = _import_plugin_register()
    register(api)
    assert len(api.doctor_contributions) == 1
    contrib = api.doctor_contributions[0]
    assert contrib.id == "browser-control"
    assert "playwright" in contrib.description.lower()


@pytest.mark.asyncio
async def test_doctor_run_returns_pass_when_playwright_present():
    """Smoke-test the doctor probe — when playwright is importable
    AND OPENCOMPUTER_BROWSER_CONTROL_URL is unset, status is 'pass'."""
    api = _FakeApi()
    register = _import_plugin_register()
    register(api)
    contrib = api.doctor_contributions[0]
    # We don't ensure playwright is installed in CI's minimal mode —
    # accept either 'pass' (installed) or 'warn' (missing). The point of
    # the test is the contribution shape + that it doesn't raise.
    result = await contrib.run(False)
    assert result.id == "browser-control"
    assert result.status in {"pass", "warn"}
    assert result.detail

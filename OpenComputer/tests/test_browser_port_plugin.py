"""Tests for extensions.browser_control.plugin — register() behavior.

As of 2026-05-08 the plugin is **dormant** by default: ``register()``
short-circuits without registering any tools or a doctor row unless
``OPENCOMPUTER_USE_BROWSER_CONTROL_LEGACY=1`` is set. The new
``browser-harness`` plugin owns the browser-tool surface. These tests
pin both branches:

* Dormant default — ``register()`` is a no-op (zero tools, zero doctor).
* Legacy reactivated — ``register()`` registers Browser + 11 shims + 1
  doctor row, just like the pre-2026-05-08 contract.
"""

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
    # Don't pop anything if already loaded — conftest pre-registers the
    # extensions.browser_control package alias. The plugin's
    # _bootstrap_package_namespace is a no-op when the alias already exists.
    mod = importlib.import_module("extensions.browser_control.plugin")
    return mod.register


def test_register_dormant_by_default_registers_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without OPENCOMPUTER_USE_BROWSER_CONTROL_LEGACY=1, register() is
    a no-op. Browser-harness owns the surface; browser-control is
    loaded-but-inactive so its package namespace stays bootstrapped for
    the typed-error fallback in adapter-runner."""
    monkeypatch.delenv("OPENCOMPUTER_USE_BROWSER_CONTROL_LEGACY", raising=False)
    api = _FakeApi()
    register = _import_plugin_register()
    register(api)
    assert api.tools == []
    assert api.doctor_contributions == []


def test_register_legacy_mode_adds_browser_plus_eleven_shims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the env var set, the legacy contract is preserved exactly
    as it was pre-2026-05-08: 1 + 11 = 12 tools + 1 doctor row."""
    monkeypatch.setenv("OPENCOMPUTER_USE_BROWSER_CONTROL_LEGACY", "1")
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


def test_register_legacy_mode_adds_doctor_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENCOMPUTER_USE_BROWSER_CONTROL_LEGACY", "1")
    api = _FakeApi()
    register = _import_plugin_register()
    register(api)
    assert len(api.doctor_contributions) == 1
    contrib = api.doctor_contributions[0]
    assert contrib.id == "browser-control"
    assert "playwright" in contrib.description.lower()


@pytest.mark.asyncio
async def test_doctor_run_returns_pass_when_playwright_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Smoke-test the doctor probe under legacy reactivation — when
    playwright is importable AND OPENCOMPUTER_BROWSER_CONTROL_URL is
    unset, status is 'pass' (or 'warn' on minimal CI)."""
    monkeypatch.setenv("OPENCOMPUTER_USE_BROWSER_CONTROL_LEGACY", "1")
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

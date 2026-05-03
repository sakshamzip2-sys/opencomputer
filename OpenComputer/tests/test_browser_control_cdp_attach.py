"""CDP attach mode for browser-control's _browser_session context manager.

When OPENCOMPUTER_BROWSER_CDP_URL is set, the existing Playwright tools
(navigate_and_snapshot etc.) connect to the user's already-running Chrome
via Chrome DevTools Protocol instead of launching a fresh ephemeral browser.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _load_browser_module():
    repo = Path(__file__).resolve().parent.parent
    provider_path = repo / "extensions" / "browser-control" / "_browser_session.py"
    module_name = f"_browser_control_under_test_{id(provider_path)}"
    spec = importlib.util.spec_from_file_location(module_name, str(provider_path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


class _FakePlaywright:
    """Minimal async-context-manager that yields a fake pw object."""

    def __init__(self, fake_chromium):
        self._fake = MagicMock()
        self._fake.chromium = fake_chromium

    async def __aenter__(self):
        return self._fake

    async def __aexit__(self, *args):
        return None


def _build_fake_chromium(*, has_existing_context: bool = False):
    fake_browser = MagicMock()
    fake_context = MagicMock()
    fake_context.close = AsyncMock()
    fake_browser.close = AsyncMock()
    fake_browser.new_context = AsyncMock(return_value=fake_context)
    fake_browser.contexts = [fake_context] if has_existing_context else []

    fake_chromium = MagicMock()
    fake_chromium.connect_over_cdp = AsyncMock(return_value=fake_browser)
    fake_chromium.launch = AsyncMock(return_value=fake_browser)
    return fake_chromium, fake_browser, fake_context


@pytest.mark.asyncio
async def test_cdp_url_set_uses_connect_over_cdp(monkeypatch):
    """OPENCOMPUTER_BROWSER_CDP_URL → connect_over_cdp; chromium.launch NOT called."""
    monkeypatch.setenv("OPENCOMPUTER_BROWSER_CDP_URL", "http://localhost:9222")
    monkeypatch.delenv("OPENCOMPUTER_BROWSER_PROFILE_PATH", raising=False)

    mod = _load_browser_module()
    fake_chromium, fake_browser, fake_context = _build_fake_chromium(
        has_existing_context=True,
    )

    with patch.object(
        mod, "_import_playwright",
        return_value=lambda: _FakePlaywright(fake_chromium),
    ):
        async with mod._browser_session() as (browser, context):
            assert browser is fake_browser

    fake_chromium.connect_over_cdp.assert_awaited_once_with("http://localhost:9222")
    fake_chromium.launch.assert_not_called()


@pytest.mark.asyncio
async def test_cdp_url_unset_uses_chromium_launch(monkeypatch):
    """No env var → chromium.launch (existing behaviour); connect_over_cdp NOT called."""
    monkeypatch.delenv("OPENCOMPUTER_BROWSER_CDP_URL", raising=False)
    monkeypatch.delenv("OPENCOMPUTER_BROWSER_PROFILE_PATH", raising=False)

    mod = _load_browser_module()
    fake_chromium, fake_browser, fake_context = _build_fake_chromium()

    with patch.object(
        mod, "_import_playwright",
        return_value=lambda: _FakePlaywright(fake_chromium),
    ):
        async with mod._browser_session() as (browser, context):
            assert browser is fake_browser

    fake_chromium.launch.assert_awaited_once()
    fake_chromium.connect_over_cdp.assert_not_called()


@pytest.mark.asyncio
async def test_cdp_mode_does_not_close_browser(monkeypatch):
    """In CDP-attach mode we MUST NOT close the user's browser on exit —
    they're using it. Only close the context we created (if we created one)."""
    monkeypatch.setenv("OPENCOMPUTER_BROWSER_CDP_URL", "http://localhost:9222")
    monkeypatch.delenv("OPENCOMPUTER_BROWSER_PROFILE_PATH", raising=False)

    mod = _load_browser_module()
    fake_chromium, fake_browser, fake_context = _build_fake_chromium(
        has_existing_context=True,
    )

    with patch.object(
        mod, "_import_playwright",
        return_value=lambda: _FakePlaywright(fake_chromium),
    ):
        async with mod._browser_session() as (browser, context):
            pass

    # The user's browser must NOT be closed in CDP mode.
    fake_browser.close.assert_not_called()

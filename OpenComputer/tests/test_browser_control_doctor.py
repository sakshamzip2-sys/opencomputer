"""tests/test_browser_control_doctor.py — doctor preflight for browser-control.

Tests the three branches of ``_check_browser_control_capable``:

1. ``playwright`` not installed → info level (opt-in, doctor stays green).
2. ``playwright`` installed AND ``async_api`` loadable → ok.
3. ``playwright`` installed but ``async_api`` not loadable → warning
   (likely environment corruption / partial install).
"""
from __future__ import annotations

from unittest.mock import patch

from opencomputer.doctor import _check_browser_control_capable


def _real_import():
    """Resolve the real builtin __import__ regardless of __builtins__ shape."""
    if isinstance(__builtins__, dict):
        return __builtins__["__import__"]
    return __builtins__.__import__


def test_playwright_missing_returns_info():
    """No playwright installed → info-level CheckResult (opt-in extra)."""
    real_import = _real_import()

    def fake_import(name, *args, **kwargs):
        if name == "playwright" or name.startswith("playwright."):
            raise ImportError("no playwright")
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=fake_import):
        result = _check_browser_control_capable()
    assert result.ok is False
    assert result.level == "info"
    assert "playwright" in result.message.lower()
    assert "browser-control" in result.message


def test_playwright_present_returns_ok():
    """Both ``playwright`` + ``playwright.async_api`` importable → ok."""
    fake_playwright = type("Pkg", (), {})()
    fake_async_api = type(
        "Pkg", (), {"async_playwright": staticmethod(lambda: None)}
    )()

    real_import = _real_import()

    def fake_import(name, *args, **kwargs):
        if name == "playwright":
            return fake_playwright
        if name == "playwright.async_api":
            return fake_async_api
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=fake_import):
        result = _check_browser_control_capable()
    assert result.ok is True
    assert result.level == "info"
    assert "browser-control" in result.message
    assert "playwright" in result.message.lower()


def test_playwright_partial_install_warns():
    """``playwright`` pkg installed but ``async_api`` not loadable → warning."""
    fake_playwright = type("Pkg", (), {})()

    real_import = _real_import()

    def fake_import(name, *args, **kwargs):
        if name == "playwright":
            return fake_playwright
        if name == "playwright.async_api":
            raise ImportError("missing async_api")
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=fake_import):
        result = _check_browser_control_capable()
    assert result.ok is False
    assert result.level == "warning"
    assert "async_api" in result.message

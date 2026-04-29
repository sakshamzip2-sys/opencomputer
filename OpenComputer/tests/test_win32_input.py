"""Win32 SendInput shim — tests are import-shape only on non-Windows
because we don't want to actually move the mouse during CI. The full
behavior is exercised on Windows via integration in test_system_click."""
from __future__ import annotations

import sys

import pytest


def test_module_imports_on_non_windows() -> None:
    """Module must be importable on macOS/Linux without raising — the
    actual SendInput call sites guard with sys.platform == 'win32'."""
    from opencomputer.tools import _win32_input

    assert hasattr(_win32_input, "click_at")
    assert hasattr(_win32_input, "type_text")
    assert hasattr(_win32_input, "send_keys")


def test_click_at_returns_false_on_non_windows() -> None:
    from opencomputer.tools._win32_input import click_at
    if sys.platform == "win32":
        pytest.skip("only tests the non-windows guard")
    assert click_at(100, 200, button="left", double=False) is False


def test_type_text_returns_false_on_non_windows() -> None:
    from opencomputer.tools._win32_input import type_text
    if sys.platform == "win32":
        pytest.skip("only tests the non-windows guard")
    assert type_text("hello") is False


def test_send_keys_stub_returns_false() -> None:
    """send_keys is an explicit stub — must return False so callers fall
    through to pyautogui."""
    from opencomputer.tools._win32_input import send_keys
    assert send_keys(["ctrl", "c"]) is False

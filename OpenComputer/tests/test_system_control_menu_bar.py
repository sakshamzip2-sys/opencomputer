"""Tests for ``opencomputer.system_control.menu_bar`` (Phase 3.F)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from opencomputer.system_control.menu_bar import (
    MenuBarIndicator,
    is_menu_bar_supported,
)


def test_is_menu_bar_supported_false_on_non_darwin() -> None:
    """Non-Darwin → unsupported regardless of rumps."""
    with patch("opencomputer.system_control.menu_bar.platform.system", return_value="Linux"):
        assert is_menu_bar_supported() is False


def test_menu_bar_supported_requires_rumps() -> None:
    """Darwin without rumps → unsupported."""

    real_import = __import__

    def _no_rumps_import(name, *args, **kwargs):
        if name == "rumps" or name.startswith("rumps."):
            raise ImportError("no rumps")
        return real_import(name, *args, **kwargs)

    with (
        patch(
            "opencomputer.system_control.menu_bar.platform.system",
            return_value="Darwin",
        ),
        patch(
            "opencomputer.system_control.menu_bar.importlib.import_module",
            side_effect=ImportError("no rumps"),
        ),
    ):
        assert is_menu_bar_supported() is False


def test_indicator_methods_no_op_when_unsupported() -> None:
    """MenuBarIndicator.start() raises a clean RuntimeError when unsupported."""
    indicator = MenuBarIndicator()
    with patch(
        "opencomputer.system_control.menu_bar.is_menu_bar_supported",
        return_value=False,
    ):
        with pytest.raises(RuntimeError, match="rumps"):
            indicator.start()


def test_indicator_stop_is_safe_when_never_started() -> None:
    """Calling stop() before start() must not raise."""
    indicator = MenuBarIndicator()
    indicator.stop()  # no exception


def test_is_menu_bar_supported_true_on_darwin_with_rumps() -> None:
    """Darwin + rumps importable → supported."""

    class _DummyRumps:
        App = object  # we never instantiate in the supported-check

    with (
        patch(
            "opencomputer.system_control.menu_bar.platform.system",
            return_value="Darwin",
        ),
        patch(
            "opencomputer.system_control.menu_bar.importlib.import_module",
            return_value=_DummyRumps(),
        ),
    ):
        assert is_menu_bar_supported() is True

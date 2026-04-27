"""tests/test_ambient_doctor_checks.py"""
from __future__ import annotations

import sys
import time
from unittest.mock import patch

import pytest

from opencomputer.doctor import _check_ambient_foreground_capable, _check_ambient_state


def test_state_missing_returns_ok_and_disabled(tmp_path):
    result = _check_ambient_state(tmp_path)
    assert result.ok is True
    assert "disabled" in result.message.lower()


def test_state_enabled_with_fresh_heartbeat_is_ok(tmp_path):
    (tmp_path / "ambient").mkdir()
    (tmp_path / "ambient" / "state.json").write_text(
        '{"enabled": true, "paused_until": null, "sensors": ["foreground"]}'
    )
    (tmp_path / "ambient" / "heartbeat").write_text(str(time.time()))
    result = _check_ambient_state(tmp_path)
    assert result.ok is True


def test_state_enabled_with_stale_heartbeat_warns(tmp_path):
    (tmp_path / "ambient").mkdir()
    (tmp_path / "ambient" / "state.json").write_text(
        '{"enabled": true, "paused_until": null, "sensors": ["foreground"]}'
    )
    (tmp_path / "ambient" / "heartbeat").write_text(str(time.time() - 600))
    result = _check_ambient_state(tmp_path)
    assert result.ok is False
    assert result.level == "warning"
    assert "stale" in result.message.lower() or "stuck" in result.message.lower()


def test_state_enabled_no_heartbeat_warns(tmp_path):
    (tmp_path / "ambient").mkdir()
    (tmp_path / "ambient" / "state.json").write_text(
        '{"enabled": true, "paused_until": null, "sensors": ["foreground"]}'
    )
    result = _check_ambient_state(tmp_path)
    assert result.ok is False
    assert result.level == "warning"


def test_state_corrupt_json_warns(tmp_path):
    (tmp_path / "ambient").mkdir()
    (tmp_path / "ambient" / "state.json").write_text("{this is not json")
    result = _check_ambient_state(tmp_path)
    assert result.ok is False


@pytest.mark.skipif(sys.platform != "darwin", reason="darwin-only path")
def test_macos_capability_check_with_osascript_present():
    with patch("opencomputer.doctor.shutil.which", return_value="/usr/bin/osascript"):
        result = _check_ambient_foreground_capable()
    assert result.ok is True


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="linux-only path")
def test_linux_warns_on_wayland(monkeypatch):
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.delenv("DISPLAY", raising=False)
    result = _check_ambient_foreground_capable()
    assert result.ok is False
    assert "wayland" in result.message.lower()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="linux-only path")
def test_linux_ok_when_xdotool_present(monkeypatch):
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    with patch("opencomputer.doctor.shutil.which", side_effect=lambda c: "/usr/bin/xdotool" if c == "xdotool" else None):
        result = _check_ambient_foreground_capable()
    assert result.ok is True

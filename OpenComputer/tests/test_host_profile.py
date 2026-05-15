"""Tests for ``plugin_sdk.host_profile`` — the startup host fingerprint.

Covers: real detection on the running host, process-caching identity,
graceful degradation when a probe raises, and the platform-specific
``display_server`` resolution logic.
"""

from __future__ import annotations

import platform

import pytest

from plugin_sdk import HostProfile, detect_host
from plugin_sdk.host_profile import (
    _detect_host_uncached,
    _probe_display_server,
)

# ─── Real detection on this host ───────────────────────────────────────


def test_detect_host_returns_populated_profile() -> None:
    """``detect_host`` yields a sane, fully-populated profile here."""
    host = detect_host()

    assert isinstance(host, HostProfile)
    assert host.os_name and host.os_name != "unknown"
    assert host.os_name == platform.system()
    assert host.os_pretty and host.os_pretty != "unknown"
    assert host.arch and host.arch != "unknown"
    assert host.python_version == platform.python_version()
    assert host.cpu_logical > 0
    assert host.cpu_physical >= 0  # some hosts can't determine physical
    assert host.total_ram_gb > 0
    assert host.hostname and host.hostname != "unknown"
    assert host.display_server in (
        "wayland",
        "x11",
        "aqua",
        "windows",
        "none",
    )
    # is_headless is exactly "no display server".
    assert host.is_headless == (host.display_server == "none")
    assert isinstance(host.is_container, bool)
    assert isinstance(host.is_wsl, bool)


def test_summary_line_is_compact_one_liner() -> None:
    """``summary_line`` returns a single non-empty line."""
    line = detect_host().summary_line()
    assert line
    assert "\n" not in line
    assert detect_host().arch in line


# ─── Process caching ───────────────────────────────────────────────────


def test_detect_host_is_cached_same_instance() -> None:
    """Repeated calls return the *same* cached instance."""
    assert detect_host() is detect_host()


# ─── Graceful degradation ──────────────────────────────────────────────


def test_degrades_when_ram_probe_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failing probe degrades only its field — no exception escapes."""
    import psutil

    def _boom() -> object:
        raise RuntimeError("simulated psutil failure")

    monkeypatch.setattr(psutil, "virtual_memory", _boom)

    # Use the uncached variant so the broken probe is actually re-run
    # (and the process-wide cache stays clean for other tests).
    host = _detect_host_uncached()

    assert isinstance(host, HostProfile)
    assert host.total_ram_gb == 0.0  # safe default
    # Other fields still detected fine.
    assert host.os_name == platform.system()
    assert host.arch and host.arch != "unknown"


def test_degrades_when_machine_probe_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing ``platform.machine`` degrades ``arch`` to 'unknown'."""

    def _boom() -> str:
        raise RuntimeError("simulated platform failure")

    monkeypatch.setattr(platform, "machine", _boom)
    host = _detect_host_uncached()
    assert host.arch == "unknown"
    assert host.cpu_logical > 0  # unaffected probe still works


# ─── display_server resolution logic ───────────────────────────────────


def test_display_server_macos() -> None:
    assert _probe_display_server("Darwin") == "aqua"


def test_display_server_windows() -> None:
    assert _probe_display_server("Windows") == "windows"


def test_display_server_linux_wayland(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)
    assert _probe_display_server("Linux") == "wayland"


def test_display_server_linux_xdg_session_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    monkeypatch.delenv("DISPLAY", raising=False)
    assert _probe_display_server("Linux") == "x11"


def test_display_server_linux_display_implies_x11(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)
    monkeypatch.setenv("DISPLAY", ":0")
    assert _probe_display_server("Linux") == "x11"


def test_display_server_linux_headless(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Linux with no display env vars resolves to 'none' (headless)."""
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)
    assert _probe_display_server("Linux") == "none"


def test_full_detect_honors_patched_platform_and_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: patch ``platform.system`` + env → headless Linux."""
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)

    host = _detect_host_uncached()
    assert host.os_name == "Linux"
    assert host.display_server == "none"
    assert host.is_headless is True

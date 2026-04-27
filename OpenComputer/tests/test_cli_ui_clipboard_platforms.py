"""Cross-platform clipboard module tests with mocked subprocesses.

The real clipboard requires interactive user input (paste an image)
which CI can't simulate. These tests mock ``subprocess.run`` so the
Linux / Windows / macOS code paths execute and we catch logic bugs
that the macOS-only manual smoke can't surface.

Mocking strategy: ``unittest.mock.patch`` over ``subprocess.run`` to
return fake ``CompletedProcess`` instances that simulate each
platform's tool output (``wl-paste --list-types``, ``xclip -t TARGETS``,
etc.). We're testing the *adapter logic*, not subprocess behavior — so
mocking is the correct boundary.
"""
from __future__ import annotations

import base64
from pathlib import Path
from unittest.mock import MagicMock, patch

from opencomputer.cli_ui.clipboard import (
    _macos_has_image,
    _macos_pngpaste,
    _wayland_has_image,
    _wayland_save,
    _windows_save,
    _xclip_has_image,
    _xclip_save,
    has_clipboard_image,
    save_clipboard_image,
)

# ── macOS paths ───────────────────────────────────────────────────────


def test_macos_has_image_detects_png_class():
    fake = MagicMock()
    fake.stdout = "«class PNGf»"
    with patch("subprocess.run", return_value=fake):
        assert _macos_has_image() is True


def test_macos_has_image_detects_tiff_class():
    fake = MagicMock()
    fake.stdout = "«class TIFF»"
    with patch("subprocess.run", return_value=fake):
        assert _macos_has_image() is True


def test_macos_has_image_returns_false_when_no_image():
    fake = MagicMock()
    fake.stdout = "«class utf8»"
    with patch("subprocess.run", return_value=fake):
        assert _macos_has_image() is False


def test_macos_has_image_handles_subprocess_failure():
    with patch("subprocess.run", side_effect=FileNotFoundError):
        assert _macos_has_image() is False


def test_macos_pngpaste_success(tmp_path: Path):
    dest = tmp_path / "out.png"
    fake = MagicMock()
    fake.returncode = 0

    def _fake_run(cmd, **kwargs):  # noqa: ANN001
        dest.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 100)
        return fake

    with patch("subprocess.run", side_effect=_fake_run):
        assert _macos_pngpaste(dest) is True
        assert dest.exists() and dest.stat().st_size > 0


def test_macos_pngpaste_not_installed(tmp_path: Path):
    dest = tmp_path / "out.png"
    with patch("subprocess.run", side_effect=FileNotFoundError):
        assert _macos_pngpaste(dest) is False


# ── Linux Wayland paths ───────────────────────────────────────────────


def test_wayland_has_image_detects_png():
    fake = MagicMock()
    fake.stdout = "image/png\nimage/jpeg\nUTF8_STRING\n"
    with patch("subprocess.run", return_value=fake):
        assert _wayland_has_image() is True


def test_wayland_has_image_no_image():
    fake = MagicMock()
    fake.stdout = "UTF8_STRING\nTEXT\n"
    with patch("subprocess.run", return_value=fake):
        assert _wayland_has_image() is False


def test_wayland_has_image_no_wl_paste():
    with patch("subprocess.run", side_effect=FileNotFoundError):
        assert _wayland_has_image() is False


def test_wayland_save_writes_bytes(tmp_path: Path):
    dest = tmp_path / "out.png"
    image_bytes = b"\x89PNG\r\n\x1a\n" + b"y" * 50
    call_count = {"n": 0}

    def _fake_run(cmd, **kwargs):  # noqa: ANN001
        call_count["n"] += 1
        if "--list-types" in cmd:
            r = MagicMock()
            r.stdout = "image/png\n"
            return r
        r = MagicMock()
        r.returncode = 0
        r.stdout = image_bytes
        return r

    with patch("subprocess.run", side_effect=_fake_run):
        assert _wayland_save(dest) is True
        assert dest.read_bytes() == image_bytes


# ── Linux X11 (xclip) paths ───────────────────────────────────────────


def test_xclip_has_image_detects_png():
    fake = MagicMock()
    fake.stdout = "TEXT\nimage/png\nimage/jpeg\n"
    with patch("subprocess.run", return_value=fake):
        assert _xclip_has_image() is True


def test_xclip_has_image_no_image():
    fake = MagicMock()
    fake.stdout = "TEXT\nUTF8_STRING\n"
    with patch("subprocess.run", return_value=fake):
        assert _xclip_has_image() is False


def test_xclip_has_image_xclip_missing():
    with patch("subprocess.run", side_effect=FileNotFoundError):
        assert _xclip_has_image() is False


def test_xclip_save_writes_bytes(tmp_path: Path):
    dest = tmp_path / "out.png"
    image_bytes = b"\x89PNG\r\n\x1a\n" + b"z" * 75

    def _fake_run(cmd, **kwargs):  # noqa: ANN001
        if "TARGETS" in cmd:
            r = MagicMock()
            r.stdout = "image/png\n"
            return r
        r = MagicMock()
        r.returncode = 0
        r.stdout = image_bytes
        return r

    with patch("subprocess.run", side_effect=_fake_run):
        assert _xclip_save(dest) is True
        assert dest.read_bytes() == image_bytes


# ── Windows (PowerShell) ──────────────────────────────────────────────


def test_windows_save_with_powershell(tmp_path: Path):
    dest = tmp_path / "out.png"
    image_bytes = b"\x89PNG\r\n\x1a\n" + b"w" * 60
    b64 = base64.b64encode(image_bytes).decode("ascii")
    call_count = {"n": 0}

    def _fake_run(cmd, **kwargs):  # noqa: ANN001
        call_count["n"] += 1
        if call_count["n"] == 1:
            r = MagicMock()
            r.returncode = 0
            return r
        r = MagicMock()
        r.returncode = 0
        r.stdout = b64
        return r

    with patch("subprocess.run", side_effect=_fake_run):
        assert _windows_save(dest) is True
        assert dest.read_bytes() == image_bytes


def test_windows_save_no_powershell(tmp_path: Path):
    dest = tmp_path / "out.png"
    with patch("subprocess.run", side_effect=FileNotFoundError):
        assert _windows_save(dest) is False


# ── Top-level dispatch (platform-aware) ───────────────────────────────


def test_save_clipboard_image_creates_parent(tmp_path: Path):
    """``save_clipboard_image`` must create the destination directory
    even when the OS-specific path returns False (no image available)."""
    target_dir = tmp_path / "deep" / "nested" / "dir"
    dest = target_dir / "out.png"
    assert not target_dir.exists()
    with patch("subprocess.run", side_effect=FileNotFoundError):
        save_clipboard_image(dest)
    assert target_dir.exists()


def test_has_clipboard_image_handles_all_platforms_safely():
    """Whatever the platform, ``has_clipboard_image`` must return a bool
    and never raise — even when every probe fails."""
    with patch("subprocess.run", side_effect=FileNotFoundError):
        result = has_clipboard_image()
        assert isinstance(result, bool)

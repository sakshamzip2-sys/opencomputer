"""Cross-platform clipboard image extraction.

Provides ``save_clipboard_image(dest)`` and ``has_clipboard_image()``. Both
shell out to OS-native CLI tools so we have ZERO Python deps — works the
moment the user pastes from their clipboard.

Platform support today:
  macOS   — osascript (always present); ``pngpaste`` (brew, fast path)
  Linux   — Wayland (``wl-paste``) → X11 (``xclip``)
  Windows — PowerShell WinForms ``Clipboard::GetImage``

Adapted from hermes-agent's ``hermes_cli/clipboard.py`` with the WSL2 +
filedrop paths trimmed (acceptable Phase 2.A scope; can be re-added if
demand surfaces).
"""
from __future__ import annotations

import base64
import logging
import os
import subprocess
import sys
from pathlib import Path

_log = logging.getLogger("opencomputer.cli_ui.clipboard")


def save_clipboard_image(dest: Path) -> bool:
    """Extract an image from the system clipboard and save it as PNG.

    Returns True iff an image was found and a file was written. Caller
    must ``dest.parent.mkdir(parents=True, exist_ok=True)`` if uncertain
    the parent exists; we attempt it defensively here.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        return _macos_save(dest)
    if sys.platform == "win32":
        return _windows_save(dest)
    return _linux_save(dest)


def has_clipboard_image() -> bool:
    """Quick check — does the clipboard currently contain an image?"""
    if sys.platform == "darwin":
        return _macos_has_image()
    if sys.platform == "win32":
        return _windows_has_image()
    if os.environ.get("WAYLAND_DISPLAY"):
        return _wayland_has_image()
    return _xclip_has_image()


# ── macOS ────────────────────────────────────────────────────────────────


def _macos_save(dest: Path) -> bool:
    """Try pngpaste first (fast, fewer format constraints), fall back to osascript."""
    return _macos_pngpaste(dest) or _macos_osascript(dest)


def _macos_has_image() -> bool:
    try:
        info = subprocess.run(
            ["osascript", "-e", "clipboard info"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        return "«class PNGf»" in info.stdout or "«class TIFF»" in info.stdout
    except Exception:
        return False


def _macos_pngpaste(dest: Path) -> bool:
    try:
        r = subprocess.run(
            ["pngpaste", str(dest)],
            capture_output=True,
            timeout=3,
            check=False,
        )
        return r.returncode == 0 and dest.exists() and dest.stat().st_size > 0
    except FileNotFoundError:
        return False
    except Exception as e:
        _log.debug("pngpaste failed: %s", e)
        return False


def _macos_osascript(dest: Path) -> bool:
    if not _macos_has_image():
        return False
    script = (
        "try\n"
        "  set imgData to the clipboard as «class PNGf»\n"
        f'  set f to open for access POSIX file "{dest}" with write permission\n'
        "  write imgData to f\n"
        "  close access f\n"
        "on error\n"
        '  return "fail"\n'
        "end try\n"
    )
    try:
        r = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return (
            r.returncode == 0
            and "fail" not in r.stdout
            and dest.exists()
            and dest.stat().st_size > 0
        )
    except Exception as e:
        _log.debug("osascript clipboard extract failed: %s", e)
        return False


# ── Linux (Wayland / X11) ────────────────────────────────────────────────


def _linux_save(dest: Path) -> bool:
    if os.environ.get("WAYLAND_DISPLAY") and _wayland_save(dest):
        return True
    return _xclip_save(dest)


def _wayland_has_image() -> bool:
    try:
        r = subprocess.run(
            ["wl-paste", "--list-types"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        return any(
            t.strip() in ("image/png", "image/jpeg", "image/jpg")
            for t in r.stdout.splitlines()
        )
    except Exception:
        return False


def _wayland_save(dest: Path) -> bool:
    if not _wayland_has_image():
        return False
    try:
        r = subprocess.run(
            ["wl-paste", "--type", "image/png"],
            capture_output=True,
            timeout=5,
            check=False,
        )
        if r.returncode == 0 and r.stdout:
            dest.write_bytes(r.stdout)
            return True
    except Exception as e:
        _log.debug("wl-paste failed: %s", e)
    return False


def _xclip_has_image() -> bool:
    try:
        r = subprocess.run(
            ["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        return any(
            t.strip() in ("image/png", "image/jpeg")
            for t in r.stdout.splitlines()
        )
    except FileNotFoundError:
        return False
    except Exception:
        return False


def _xclip_save(dest: Path) -> bool:
    if not _xclip_has_image():
        return False
    try:
        r = subprocess.run(
            ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"],
            capture_output=True,
            timeout=5,
            check=False,
        )
        if r.returncode == 0 and r.stdout:
            dest.write_bytes(r.stdout)
            return True
    except Exception as e:
        _log.debug("xclip failed: %s", e)
    return False


# ── Windows (PowerShell) ─────────────────────────────────────────────────

_PS_GET_IMAGE = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
$img = [System.Windows.Forms.Clipboard]::GetImage()
if ($null -eq $img) { exit 1 }
$ms = New-Object System.IO.MemoryStream
$img.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)
[Convert]::ToBase64String($ms.ToArray()) | Write-Output
"""


def _find_powershell() -> str | None:
    for exe in ("pwsh", "powershell"):
        try:
            r = subprocess.run(
                [exe, "-NoProfile", "-Command", "exit 0"],
                capture_output=True,
                timeout=3,
                check=False,
            )
            if r.returncode == 0:
                return exe
        except FileNotFoundError:
            continue
        except Exception:
            continue
    return None


def _windows_has_image() -> bool:
    return _find_powershell() is not None


def _windows_save(dest: Path) -> bool:
    ps = _find_powershell()
    if ps is None:
        return False
    try:
        r = subprocess.run(
            [ps, "-NoProfile", "-Command", _PS_GET_IMAGE],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        if r.returncode != 0:
            return False
        b64 = r.stdout.strip()
        if not b64:
            return False
        dest.write_bytes(base64.b64decode(b64))
        return dest.stat().st_size > 0
    except Exception as e:
        _log.debug("powershell clipboard extract failed: %s", e)
        return False

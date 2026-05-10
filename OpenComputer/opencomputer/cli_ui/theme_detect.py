"""Light / dark terminal detection.

Hermes-CLI parity (doc lines 318-325). Layered detection — env override
first, then COLORFGBG, then an OSC 11 background-colour query. The OSC 11
probe runs once at launch with a 200 ms read timeout; dumb terminals fail
silently and we fall back to dark.
"""

from __future__ import annotations

import os
import re
import select
import sys
from collections.abc import Callable
from dataclasses import dataclass

_HEX_RE = re.compile(r"^[0-9a-fA-F]{6}$")
_OSC11_RE = re.compile(r"rgb:([0-9a-fA-F]+)/([0-9a-fA-F]+)/([0-9a-fA-F]+)")


@dataclass(frozen=True)
class Theme:
    kind: str  # "light" | "dark"
    bg_hex: str = ""  # 6-hex bg colour, "" if unknown


def _hex_to_luminance(hx: str) -> float:
    """Approximate perceptual luminance of a 6-hex bg colour, [0,1]."""
    r = int(hx[0:2], 16) / 255.0
    g = int(hx[2:4], 16) / 255.0
    b = int(hx[4:6], 16) / 255.0
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _from_env() -> Theme | None:
    val = os.environ.get("OPENCOMPUTER_TUI_THEME", "").strip().lower()
    if val == "light":
        return Theme(kind="light", bg_hex="ffffff")
    if val == "dark":
        return Theme(kind="dark", bg_hex="000000")
    if _HEX_RE.match(val):
        kind = "light" if _hex_to_luminance(val) > 0.5 else "dark"
        return Theme(kind=kind, bg_hex=val)
    return None


def _from_colorfgbg() -> Theme | None:
    val = os.environ.get("COLORFGBG", "").strip()
    if not val:
        return None
    parts = val.split(";")
    if len(parts) < 2:
        return None
    try:
        bg = int(parts[-1])
    except ValueError:
        return None
    # xterm convention: 0-7 dark, 8-15 light, 15 = white.
    return Theme(kind="light" if bg >= 8 else "dark", bg_hex="")


def _parse_osc11(reply: str) -> Theme | None:
    m = _OSC11_RE.search(reply)
    if not m:
        return None

    def _hi(s: str) -> str:
        return (s + "00")[:2]

    hx = _hi(m.group(1)) + _hi(m.group(2)) + _hi(m.group(3))
    kind = "light" if _hex_to_luminance(hx) > 0.5 else "dark"
    return Theme(kind=kind, bg_hex=hx)


def _osc11_probe_real(timeout_ms: int = 200) -> str | None:
    """Send OSC 11 query, read reply with a strict timeout.

    Returns the reply string, or ``None`` if the terminal didn't respond.
    Wrapped in best-effort try/except — never raises.
    """
    if not sys.stdout.isatty() or not sys.stdin.isatty():
        return None
    try:
        import termios
        import tty
    except ImportError:
        return None  # Windows
    try:
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
    except Exception:  # noqa: BLE001
        return None
    try:
        tty.setcbreak(fd)
        sys.stdout.write("\x1b]11;?\x1b\\")
        sys.stdout.flush()
        end_at = timeout_ms / 1000.0
        buf = ""
        import time as _t
        start = _t.monotonic()
        while _t.monotonic() - start < end_at:
            r, _, _ = select.select(
                [fd], [], [], end_at - (_t.monotonic() - start)
            )
            if not r:
                break
            ch = os.read(fd, 1).decode("ascii", errors="ignore")
            buf += ch
            if buf.endswith("\x1b\\") or buf.endswith("\x07"):
                break
        return buf or None
    except Exception:  # noqa: BLE001
        return None
    finally:
        try:
            termios.tcsetattr(fd, termios.TCSANOW, old)
        except Exception:  # noqa: BLE001
            pass


def detect_theme(
    *, probe: Callable[[], str | None] | None = None
) -> Theme:
    """Best-effort theme detection. Pure for unit-testing via *probe*.

    Detection layers (highest priority first):

    1. ``OPENCOMPUTER_TUI_THEME`` env var (``light`` | ``dark`` | 6-hex bg).
    2. ``COLORFGBG`` env var (xterm convention).
    3. OSC 11 background probe with 200 ms timeout.
    4. Default: dark.
    """
    t = _from_env()
    if t is not None:
        return t
    t = _from_colorfgbg()
    if t is not None:
        return t
    p = probe if probe is not None else _osc11_probe_real
    reply = p()
    if reply:
        t = _parse_osc11(reply)
        if t is not None:
            return t
    return Theme(kind="dark", bg_hex="000000")

"""Chrome profile decoration — atomic mutation of `Local State` and `Default/Preferences`.

OpenClaw decorates managed user-data-dirs so the user can visually distinguish
agent-controlled windows from their personal Chrome (custom profile name + theme
color). OpenClaw's TS source writes both prefs files non-atomically (`fs.writeFileSync`
+ no rename); a crash mid-write corrupts the user's profile. We use atomic_write_json
exclusively.

Best-effort: a failure here does NOT raise — Chrome works fine without decoration.
The caller (launch.py) logs and proceeds.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .._utils.atomic_write import atomic_write_json

_log = logging.getLogger("opencomputer.browser_control.chrome.decoration")

_LOCAL_STATE = "Local State"
_DEFAULT_PREFS = "Default/Preferences"


# ─── color conversion ────────────────────────────────────────────────


def parse_hex_rgb_to_signed_argb_int(hex_str: str) -> int | None:
    """`#FF4500` -> JS-signed 32-bit int with 0xFF alpha (Chrome's SkColor encoding).

    Values > 0x7FFFFFFF wrap to negative — that's what Chrome's preferences JSON
    actually contains. Returns None if the input isn't 6-hex-digits with optional `#`.
    """
    if not isinstance(hex_str, str):
        return None
    s = hex_str.strip()
    if not s:
        return None
    if s.startswith("#"):
        s = s[1:]
    if len(s) != 6:
        return None
    try:
        rgb = int(s, 16)
    except ValueError:
        return None
    argb = (0xFF << 24) | rgb
    return argb - 0x1_0000_0000 if argb > 0x7FFFFFFF else argb


# ─── deep-set mutation helper ────────────────────────────────────────


def _set_deep(d: dict[str, Any], keys: list[str], value: Any) -> None:
    """Walk/create intermediate dicts for keys[:-1], then set keys[-1]=value.

    Mirrors OpenClaw's setDeep — replaces non-dict intermediates with fresh dicts
    so a corrupted/scalar branch doesn't trip TypeError.
    """
    node: dict[str, Any] = d
    for k in keys[:-1]:
        nxt = node.get(k)
        if not isinstance(nxt, dict):
            nxt = {}
            node[k] = nxt
        node = nxt
    node[keys[-1]] = value


def _safe_read_json(path: Path) -> dict[str, Any] | None:
    """Read JSON; return None on missing / parse failure / non-object root."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError) as exc:
        _log.debug("safe_read_json(%s): read failed: %s", path, exc)
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        _log.debug("safe_read_json(%s): parse failed: %s", path, exc)
        return None
    if not isinstance(data, dict):
        return None
    return data


# ─── public API ──────────────────────────────────────────────────────


def is_profile_decorated(
    user_data_dir: str,
    desired_name: str,
    desired_color_hex: str,
) -> bool:
    """Return True iff name and color match what `decorate_openclaw_profile` would write."""
    base = Path(user_data_dir)
    local_state = _safe_read_json(base / _LOCAL_STATE) or {}
    default_prefs = _safe_read_json(base / _DEFAULT_PREFS) or {}
    desired_argb = parse_hex_rgb_to_signed_argb_int(desired_color_hex)
    desired_norm = "#" + desired_color_hex.lstrip("#").upper() if desired_color_hex else ""

    # Local State checks
    info = (
        local_state.get("profile", {}).get("info_cache", {}).get("Default", {})
        if isinstance(local_state.get("profile"), dict)
        else {}
    )
    if not isinstance(info, dict):
        return False
    if info.get("name") != desired_name:
        return False
    if (info.get("profile_color") or "").upper() != desired_norm:
        return False
    if desired_argb is not None and info.get("profile_color_seed") != desired_argb:
        return False

    # Default/Preferences checks
    profile = default_prefs.get("profile") if isinstance(default_prefs.get("profile"), dict) else {}
    if not isinstance(profile, dict):
        return False
    if profile.get("name") != desired_name:
        return False
    return (profile.get("profile_color") or "").upper() == desired_norm


def decorate_openclaw_profile(
    user_data_dir: str,
    *,
    name: str,
    color: str,
) -> None:
    """Mutate Local State and Default/Preferences so Chrome paints the OpenClaw badge.

    Atomic (write-tmp + fsync + rename) — fixes OpenClaw's non-atomic write bug.
    Best-effort: failures are logged but do not raise.
    """
    base = Path(user_data_dir)
    norm_color = "#" + color.lstrip("#").upper()
    argb = parse_hex_rgb_to_signed_argb_int(norm_color)

    # ── Local State ──
    try:
        local_state = _safe_read_json(base / _LOCAL_STATE) or {}
        info_path = ["profile", "info_cache", "Default"]
        _set_deep(local_state, [*info_path, "name"], name)
        _set_deep(local_state, [*info_path, "shortcut_name"], name)
        _set_deep(local_state, [*info_path, "user_name"], name)
        _set_deep(local_state, [*info_path, "profile_color"], norm_color)
        _set_deep(local_state, [*info_path, "user_color"], norm_color)
        if argb is not None:
            _set_deep(local_state, [*info_path, "profile_color_seed"], argb)
            _set_deep(local_state, [*info_path, "profile_highlight_color"], argb)
            _set_deep(local_state, [*info_path, "default_avatar_fill_color"], argb)
            _set_deep(local_state, [*info_path, "default_avatar_stroke_color"], argb)
        atomic_write_json(base / _LOCAL_STATE, local_state)
    except Exception as exc:  # noqa: BLE001 — best-effort
        _log.warning("decorate_openclaw_profile: Local State write failed: %s", exc)

    # ── Default/Preferences ──
    try:
        default_prefs = _safe_read_json(base / _DEFAULT_PREFS) or {}
        _set_deep(default_prefs, ["profile", "name"], name)
        _set_deep(default_prefs, ["profile", "profile_color"], norm_color)
        _set_deep(default_prefs, ["profile", "user_color"], norm_color)
        if argb is not None:
            _set_deep(default_prefs, ["autogenerated", "theme", "color"], argb)
            _set_deep(default_prefs, ["browser", "theme", "user_color2"], argb)
        atomic_write_json(base / _DEFAULT_PREFS, default_prefs)
    except Exception as exc:  # noqa: BLE001 — best-effort
        _log.warning("decorate_openclaw_profile: Default/Preferences write failed: %s", exc)


def ensure_profile_clean_exit(user_data_dir: str) -> None:
    """Set exit_type=Normal + exited_cleanly=true to suppress Chrome's restore bubble.

    Best-effort. Do not raise — at worst the user sees a benign "didn't shut down
    cleanly" prompt on next launch.
    """
    base = Path(user_data_dir)
    try:
        prefs = _safe_read_json(base / _DEFAULT_PREFS) or {}
        _set_deep(prefs, ["profile", "exit_type"], "Normal")
        _set_deep(prefs, ["profile", "exited_cleanly"], True)
        atomic_write_json(base / _DEFAULT_PREFS, prefs)
    except Exception as exc:  # noqa: BLE001
        _log.debug("ensure_profile_clean_exit failed: %s", exc)

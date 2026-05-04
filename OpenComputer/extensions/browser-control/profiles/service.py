"""Profile CRUD service — pure config-state layer.

Validates names, allocates ports/colors, mutates a `ResolvedBrowserConfig`
in place, and invokes a `write_config` callback to persist. Chrome lifecycle
(stop running browser, trash user-data-dir) is composed in by callers via
the `after_remove` hook — keeps `profiles/` independent of `chrome/`.

Mirrors OpenClaw's profiles-service.ts but trims the Chrome-driving paths
out; those land in W2b/W3 where the server routes wire everything together.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlparse

from .config import (
    CDP_PORT_RANGE_END,
    CDP_PORT_RANGE_START,
    DEFAULT_OPENCLAW_BROWSER_COLOR,
    DEFAULT_OPENCLAW_BROWSER_PROFILE_NAME,
    MAX_PROFILE_NAME_LENGTH,
    PROFILE_COLORS,
    PROFILE_NAME_REGEX,
    BrowserDriver,
    BrowserProfileConfig,
    ResolvedBrowserConfig,
)

_HEX6 = re.compile(r"^#[0-9A-Fa-f]{6}$")
_PROFILE_NAME_PATTERN = re.compile(PROFILE_NAME_REGEX)


class ProfileValidationError(ValueError):
    """Raised when create/delete params don't pass validation."""


# ─── value validation / allocation ────────────────────────────────────


def is_valid_profile_name(name: str) -> bool:
    if not isinstance(name, str):
        return False
    if not 1 <= len(name) <= MAX_PROFILE_NAME_LENGTH:
        return False
    return bool(_PROFILE_NAME_PATTERN.match(name))


def get_used_ports(profiles: dict[str, BrowserProfileConfig]) -> set[int]:
    used: set[int] = set()
    for cfg in profiles.values():
        if cfg.cdp_port:
            used.add(cfg.cdp_port)
        if cfg.cdp_url:
            parsed = urlparse(cfg.cdp_url)
            if parsed.port:
                used.add(parsed.port)
            elif parsed.scheme in ("https", "wss"):
                used.add(443)
            elif parsed.scheme in ("http", "ws"):
                used.add(80)
    return used


def get_used_colors(profiles: dict[str, BrowserProfileConfig]) -> set[str]:
    return {(cfg.color or "").upper() for cfg in profiles.values() if cfg.color}


def allocate_cdp_port(
    used_ports: set[int],
    *,
    start: int = CDP_PORT_RANGE_START,
    end: int = CDP_PORT_RANGE_END,
) -> int | None:
    if start <= 0 or end <= 0 or start > end:
        return None
    for port in range(start, end + 1):
        if port not in used_ports:
            return port
    return None


def allocate_color(used_colors: set[str]) -> str:
    used_upper = {c.upper() for c in used_colors}
    for candidate in PROFILE_COLORS:
        if candidate.upper() not in used_upper:
            return candidate
    # Palette exhausted — cycle.
    return PROFILE_COLORS[len(used_upper) % len(PROFILE_COLORS)]


# ─── create / delete params + results ─────────────────────────────────


@dataclass(slots=True)
class CreateProfileParams:
    name: str
    color: str | None = None
    cdp_url: str | None = None
    user_data_dir: str | None = None
    driver: BrowserDriver | None = None  # default → "managed"


@dataclass(slots=True)
class CreateProfileResult:
    profile_name: str
    transport: Literal["cdp", "chrome-mcp"]
    cdp_port: int | None
    cdp_url: str | None
    user_data_dir: str | None
    color: str
    is_remote: bool


@dataclass(slots=True)
class DeleteProfileResult:
    profile_name: str
    user_data_dir: str | None
    driver: BrowserDriver


# ─── create_profile ───────────────────────────────────────────────────


def create_profile(
    state: ResolvedBrowserConfig,
    params: CreateProfileParams,
    *,
    write_config: Callable[[ResolvedBrowserConfig], None] | None = None,
) -> CreateProfileResult:
    """Validate + add a profile to `state.profiles`. Calls write_config(state) on success."""
    name = (params.name or "").strip()
    if not is_valid_profile_name(name):
        raise ProfileValidationError(
            f"profile name {name!r} must match {PROFILE_NAME_REGEX} (≤ {MAX_PROFILE_NAME_LENGTH} chars)"
        )
    if name in state.profiles:
        raise ProfileValidationError(f"profile {name!r} already exists")

    raw_color = (params.color or "").strip() or None
    if raw_color is not None and not _HEX6.match(raw_color):
        raise ProfileValidationError(f"color {raw_color!r} must be #RRGGBB")
    color = (
        "#" + raw_color.lstrip("#").upper()
        if raw_color
        else allocate_color(get_used_colors(state.profiles))
    )

    driver: BrowserDriver = params.driver or "managed"

    user_data_dir = (params.user_data_dir or "").strip() or None
    cdp_url = (params.cdp_url or "").strip() or None

    if user_data_dir and driver != "existing-session":
        raise ProfileValidationError(
            "user_data_dir is only valid with driver='existing-session'"
        )
    if cdp_url and driver == "existing-session":
        raise ProfileValidationError(
            "cdp_url cannot be set on an existing-session profile (Chrome MCP discovers it at runtime)"
        )
    if cdp_url and driver == "control-extension":
        raise ProfileValidationError(
            "cdp_url cannot be set on a control-extension profile "
            "(extension connects via WebSocket, not CDP port)"
        )

    cdp_port: int | None = None
    transport: Literal["cdp", "chrome-mcp", "control-extension"] = "cdp"
    is_remote = False

    if driver == "existing-session":
        transport = "chrome-mcp"
        cdp_url = None
        cdp_port = None
    elif driver == "control-extension":
        transport = "control-extension"
        cdp_url = None
        cdp_port = None
    elif cdp_url:
        parsed = urlparse(cdp_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ProfileValidationError(f"cdp_url {cdp_url!r} must be http(s)://host[:port]")
        host = parsed.hostname
        cdp_port = parsed.port
        is_remote = host.lower() != "localhost" and not _is_loopback_address(host)
    else:
        used_ports = get_used_ports(state.profiles)
        allocated = allocate_cdp_port(
            used_ports,
            start=state.cdp_port_range_start,
            end=state.cdp_port_range_end,
        )
        if allocated is None:
            raise ProfileValidationError(
                f"no free CDP port in [{state.cdp_port_range_start}, {state.cdp_port_range_end}]"
            )
        cdp_port = allocated

    state.profiles[name] = BrowserProfileConfig(
        cdp_port=cdp_port,
        cdp_url=cdp_url,
        color=color,
        driver=driver,
        attach_only=True if driver in ("existing-session", "control-extension") else None,
        user_data_dir=user_data_dir,
    )
    if write_config is not None:
        write_config(state)

    return CreateProfileResult(
        profile_name=name,
        transport=transport,
        cdp_port=cdp_port,
        cdp_url=cdp_url,
        user_data_dir=user_data_dir,
        color=color,
        is_remote=is_remote,
    )


# ─── delete_profile ───────────────────────────────────────────────────


def delete_profile(
    state: ResolvedBrowserConfig,
    name: str,
    *,
    write_config: Callable[[ResolvedBrowserConfig], None] | None = None,
    after_remove: Callable[[str, BrowserProfileConfig], None] | None = None,
) -> DeleteProfileResult:
    """Remove a profile from state. Refuses to delete the default opencomputer profile.

    `after_remove(name, removed_cfg)` runs after the in-memory state is updated
    and the config is persisted — wiring point for Chrome stop + user-data-dir
    trashing in higher layers.
    """
    if name == DEFAULT_OPENCLAW_BROWSER_PROFILE_NAME or name == state.default_profile:
        raise ProfileValidationError(f"cannot delete default profile {name!r}")
    cfg = state.profiles.get(name)
    if cfg is None:
        raise ProfileValidationError(f"profile {name!r} does not exist")

    del state.profiles[name]
    if write_config is not None:
        write_config(state)
    if after_remove is not None:
        after_remove(name, cfg)

    return DeleteProfileResult(
        profile_name=name,
        user_data_dir=cfg.user_data_dir,
        driver=cfg.driver or "managed",
    )


# ─── helpers ──────────────────────────────────────────────────────────


def _is_loopback_address(host: str) -> bool:
    import ipaddress

    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


# Re-export the default color so callers can apply it without importing config.py.
__all__ = [
    "DEFAULT_OPENCLAW_BROWSER_COLOR",
]

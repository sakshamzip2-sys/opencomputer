"""Pull-based config resolver.

Two stages, mirroring OpenClaw's config.ts:

  resolve_browser_config(raw, full_config) -> ResolvedBrowserConfig
      Stage 1. Parse the `browser:` section of `~/.opencomputer/<profile>/config.yaml`,
      apply defaults, ensure default `opencomputer` and `user` profiles exist.

  resolve_profile(resolved, profile_name) -> ResolvedBrowserProfile | None
      Stage 2. Compute per-profile cdp_url / cdp_host / cdp_port and capability flags.
      Returns None if the named profile does not exist.

There is no file watcher. Hot reload is "re-call this resolver per request" —
the loader cache provides the only debounce. See deep-dive §"config.ts" and
"resolved-config-refresh.ts" for the rationale.
"""

from __future__ import annotations

import ipaddress
import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .config import (
    CDP_PORT_RANGE_END,
    CDP_PORT_RANGE_SPAN,
    CDP_PORT_RANGE_START,
    DEFAULT_BROWSER_CONTROL_PORT,
    DEFAULT_BROWSER_DEFAULT_PROFILE_NAME,
    DEFAULT_OPENCLAW_BROWSER_COLOR,
    DEFAULT_OPENCLAW_BROWSER_PROFILE_NAME,
    DEFAULT_REMOTE_CDP_HANDSHAKE_TIMEOUT_MS,
    DEFAULT_REMOTE_CDP_TIMEOUT_MS,
    DEFAULT_USER_BROWSER_PROFILE_COLOR,
    DEFAULT_USER_BROWSER_PROFILE_NAME,
    BrowserProfileConfig,
    ResolvedBrowserConfig,
    ResolvedBrowserProfile,
    SsrfPolicy,
)

_log = logging.getLogger("opencomputer.browser_control.profiles.resolver")


# ─── extra_args policy ─────────────────────────────────────────────────

# Flags that conflict with launch invariants or open security holes — rejected
# even if they appear in user config. OpenClaw shipped extraArgs unfiltered
# (see deep-dive bug list); we don't reproduce that.
_EXTRA_ARGS_FORBIDDEN_PREFIXES = (
    "--user-data-dir",
    "--remote-debugging-port",
    "--remote-debugging-pipe",
    "--remote-allow-origins",
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--load-extension",
    "--disable-web-security",
    "--proxy-server",
    "--proxy-pac-url",
    "--proxy-auto-detect",
)


def _filter_extra_args(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        flag = item.split("=", 1)[0]
        if any(flag == p or flag.startswith(p + "=") for p in _EXTRA_ARGS_FORBIDDEN_PREFIXES):
            _log.warning("rejecting forbidden extra browser arg: %r", item)
            continue
        out.append(item)
    return out


# ─── helpers ───────────────────────────────────────────────────────────

_HEX6 = re.compile(r"^#?[0-9A-Fa-f]{6}$")


def _normalize_hex_color(value: Any, default: str = DEFAULT_OPENCLAW_BROWSER_COLOR) -> str:
    if not isinstance(value, str):
        return default
    candidate = value.strip()
    if not candidate:
        return default
    if not _HEX6.match(candidate):
        return default
    if not candidate.startswith("#"):
        candidate = "#" + candidate
    return "#" + candidate[1:].upper()


def _coerce_timeout_ms(value: Any, fallback: int) -> int:
    if isinstance(value, bool):  # bool is an int subclass; reject explicitly
        return fallback
    if isinstance(value, int):
        return value if value >= 0 else fallback
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")) or value < 0:
            return fallback
        return int(value)
    return fallback


def _coerce_port_range_start(value: Any, default: int = CDP_PORT_RANGE_START) -> int:
    span = CDP_PORT_RANGE_SPAN
    if isinstance(value, bool):
        return default
    if isinstance(value, int) and 1 <= value <= 65535 - span:
        return value
    return default


def _is_loopback_host(host: str) -> bool:
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _parse_browser_http_url(raw: str) -> tuple[str, int, str] | None:
    """Return (host, port, normalized_url) or None if the URL is unparseable."""
    if not raw:
        return None
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https", "ws", "wss"):
        return None
    host = parsed.hostname or ""
    if not host:
        return None
    if parsed.port is not None:
        port = parsed.port
    else:
        port = 443 if parsed.scheme in ("https", "wss") else 80
    scheme = "https" if parsed.scheme in ("https", "wss") else "http"
    normalized = f"{scheme}://{host}:{port}"
    return host, port, normalized


_STALE_WS_PATH = re.compile(r"^wss?://", re.IGNORECASE)
_HAS_DEVTOOLS_BROWSER_PATH = re.compile(r"/devtools/browser/", re.IGNORECASE)


def _has_stale_ws_path(raw: str, cdp_port: int | None) -> bool:
    if not raw or not cdp_port or cdp_port <= 0:
        return False
    return bool(_STALE_WS_PATH.match(raw)) and bool(_HAS_DEVTOOLS_BROWSER_PATH.search(raw))


def _build_profile_config(raw: Any) -> BrowserProfileConfig | None:
    if not isinstance(raw, dict):
        return None
    driver = raw.get("driver")
    if driver not in (None, "managed", "existing-session", "control-extension"):
        return None
    cdp_port = raw.get("cdp_port") if isinstance(raw.get("cdp_port"), int) else None
    cdp_url = raw.get("cdp_url") if isinstance(raw.get("cdp_url"), str) else None
    user_data_dir = raw.get("user_data_dir") if isinstance(raw.get("user_data_dir"), str) else None
    attach_only_raw = raw.get("attach_only")
    attach_only = bool(attach_only_raw) if isinstance(attach_only_raw, bool) else None
    return BrowserProfileConfig(
        cdp_port=cdp_port,
        cdp_url=cdp_url,
        color=_normalize_hex_color(raw.get("color")),
        driver=driver,
        attach_only=attach_only,
        user_data_dir=user_data_dir,
    )


def _resolve_ssrf_policy(raw: dict[str, Any]) -> SsrfPolicy | None:
    legacy = raw.get("allow_private_network")
    modern = raw.get("dangerously_allow_private_network")
    allow = bool(legacy) if isinstance(legacy, bool) else False
    if isinstance(modern, bool):
        allow = allow or modern
    allowed = raw.get("allowed_hostnames")
    allowlist = raw.get("hostname_allowlist")
    if (
        not allow
        and not isinstance(allowed, list)
        and not isinstance(allowlist, list)
    ):
        return None
    return SsrfPolicy(
        dangerously_allow_private_network=allow,
        allowed_hostnames=[s for s in (allowed or []) if isinstance(s, str)] or None,
        hostname_allowlist=[s for s in (allowlist or []) if isinstance(s, str)] or None,
    )


def _ensure_default_profile(
    profiles: dict[str, BrowserProfileConfig],
    *,
    color: str,
    legacy_cdp_port: int | None,
    cdp_range_start: int,
    legacy_cdp_url: str | None,
) -> None:
    if DEFAULT_OPENCLAW_BROWSER_PROFILE_NAME in profiles:
        return
    profiles[DEFAULT_OPENCLAW_BROWSER_PROFILE_NAME] = BrowserProfileConfig(
        cdp_port=legacy_cdp_port if legacy_cdp_port is not None else cdp_range_start,
        cdp_url=legacy_cdp_url,
        color=color,
        driver="managed",
    )


def _ensure_default_user_profile(profiles: dict[str, BrowserProfileConfig]) -> None:
    if DEFAULT_USER_BROWSER_PROFILE_NAME in profiles:
        return
    profiles[DEFAULT_USER_BROWSER_PROFILE_NAME] = BrowserProfileConfig(
        color=DEFAULT_USER_BROWSER_PROFILE_COLOR,
        driver="existing-session",
    )


# ─── one-time disk migration: openclaw → opencomputer ────────────────


def migrate_legacy_profile_dir(
    *,
    base_dir: Path | None = None,
) -> bool:
    """Rename ``~/.opencomputer/browser/openclaw`` → ``.../opencomputer``.

    Idempotent — safe to call repeatedly. Only renames when the legacy
    directory exists AND the new directory does not. Existing users keep
    their Chrome profile state (~191MB) across the rename.

    Returns True if a rename happened, False otherwise. Errors are
    logged and swallowed so a half-broken filesystem can't wedge
    startup.

    ``base_dir`` overrides the default ``~/.opencomputer/browser`` for
    tests.
    """
    base = base_dir if base_dir is not None else Path.home() / ".opencomputer" / "browser"
    old_dir = base / "openclaw"
    new_dir = base / "opencomputer"
    try:
        if old_dir.exists() and not new_dir.exists():
            old_dir.rename(new_dir)
            _log.info(
                "migrate_legacy_profile_dir: renamed %s → %s",
                old_dir,
                new_dir,
            )
            return True
    except OSError as exc:
        _log.warning(
            "migrate_legacy_profile_dir: rename %s → %s failed: %s",
            old_dir,
            new_dir,
            exc,
        )
    return False


# ─── stage 1: resolve_browser_config ──────────────────────────────────


def resolve_browser_config(
    raw: dict[str, Any] | None,
    full_config: dict[str, Any] | None = None,
) -> ResolvedBrowserConfig:
    """Resolve the `browser:` section. Pull-based — call once per request."""
    # One-time disk migration. Idempotent and best-effort — never raises
    # so config resolution can proceed even on a borked filesystem.
    migrate_legacy_profile_dir()

    raw = raw or {}
    full_config = full_config or {}

    enabled = raw.get("enabled", True) if isinstance(raw.get("enabled"), bool) else True
    evaluate_enabled_raw = raw.get("evaluate_enabled", True)
    evaluate_enabled = (
        evaluate_enabled_raw if isinstance(evaluate_enabled_raw, bool) else True
    )

    control_port_raw = raw.get("control_port")
    control_port = (
        control_port_raw
        if isinstance(control_port_raw, int) and 1 <= control_port_raw <= 65535
        else DEFAULT_BROWSER_CONTROL_PORT
    )

    color = _normalize_hex_color(raw.get("color"))
    headless = bool(raw.get("headless")) if isinstance(raw.get("headless"), bool) else False
    no_sandbox = bool(raw.get("no_sandbox")) if isinstance(raw.get("no_sandbox"), bool) else False
    attach_only = bool(raw.get("attach_only")) if isinstance(raw.get("attach_only"), bool) else False

    remote_http_to = _coerce_timeout_ms(
        raw.get("remote_cdp_timeout_ms"), DEFAULT_REMOTE_CDP_TIMEOUT_MS
    )
    remote_hs_to = _coerce_timeout_ms(
        raw.get("remote_cdp_handshake_timeout_ms"),
        max(DEFAULT_REMOTE_CDP_HANDSHAKE_TIMEOUT_MS, remote_http_to * 2),
    )

    cdp_range_start = _coerce_port_range_start(raw.get("cdp_port_range_start"))
    cdp_range_end = min(cdp_range_start + CDP_PORT_RANGE_SPAN, CDP_PORT_RANGE_END)

    legacy_cdp_url = raw.get("cdp_url") if isinstance(raw.get("cdp_url"), str) else None
    legacy_cdp_port = (
        raw.get("cdp_port") if isinstance(raw.get("cdp_port"), int) else None
    )

    if legacy_cdp_url:
        parsed = _parse_browser_http_url(legacy_cdp_url)
        if parsed is not None:
            cdp_host, derived_port, _ = parsed
            cdp_protocol = "https" if legacy_cdp_url.lower().startswith(("https", "wss")) else "http"
        else:
            cdp_host, derived_port, cdp_protocol = "127.0.0.1", control_port + 1, "http"
    else:
        cdp_host = "127.0.0.1"
        derived_port = control_port + 1
        cdp_protocol = "http"

    cdp_is_loopback = _is_loopback_host(cdp_host)

    profiles_raw = raw.get("profiles") if isinstance(raw.get("profiles"), dict) else {}
    profiles: dict[str, BrowserProfileConfig] = {}
    for name, entry in profiles_raw.items():
        if not isinstance(name, str) or not name.strip():
            continue
        cfg = _build_profile_config(entry)
        if cfg is None:
            continue
        profiles[name] = cfg

    _ensure_default_profile(
        profiles,
        color=color,
        legacy_cdp_port=legacy_cdp_port if legacy_cdp_port is not None else derived_port,
        cdp_range_start=cdp_range_start,
        legacy_cdp_url=legacy_cdp_url,
    )
    _ensure_default_user_profile(profiles)

    default_profile_raw = raw.get("default_profile")
    if isinstance(default_profile_raw, str) and default_profile_raw in profiles:
        default_profile = default_profile_raw
    elif DEFAULT_OPENCLAW_BROWSER_PROFILE_NAME in profiles:
        default_profile = DEFAULT_OPENCLAW_BROWSER_PROFILE_NAME
    elif DEFAULT_USER_BROWSER_PROFILE_NAME in profiles:
        default_profile = DEFAULT_USER_BROWSER_PROFILE_NAME
    else:
        default_profile = DEFAULT_BROWSER_DEFAULT_PROFILE_NAME

    return ResolvedBrowserConfig(
        enabled=enabled,
        evaluate_enabled=evaluate_enabled,
        control_port=control_port,
        cdp_port_range_start=cdp_range_start,
        cdp_port_range_end=cdp_range_end,
        cdp_protocol=cdp_protocol,
        cdp_host=cdp_host,
        cdp_is_loopback=cdp_is_loopback,
        remote_cdp_timeout_ms=remote_http_to,
        remote_cdp_handshake_timeout_ms=remote_hs_to,
        color=color,
        executable_path=(
            raw.get("executable_path") if isinstance(raw.get("executable_path"), str) else None
        ),
        headless=headless,
        no_sandbox=no_sandbox,
        attach_only=attach_only,
        default_profile=default_profile,
        profiles=profiles,
        ssrf_policy=_resolve_ssrf_policy(raw),
        extra_args=_filter_extra_args(raw.get("extra_args")),
    )


# ─── stage 2: resolve_profile ─────────────────────────────────────────


def resolve_profile(
    resolved: ResolvedBrowserConfig,
    profile_name: str,
) -> ResolvedBrowserProfile | None:
    """Resolve one profile by name. Returns None if the name is not present."""
    profile = resolved.profiles.get(profile_name)
    if profile is None:
        return None

    if profile.driver == "existing-session":
        # Chrome MCP attaches to the user's running Chrome at runtime; CDP
        # endpoint is unknown until then. Mirror OpenClaw's "all zero" output.
        return ResolvedBrowserProfile(
            name=profile_name,
            cdp_port=0,
            cdp_url="",
            cdp_host="",
            cdp_is_loopback=True,
            user_data_dir=profile.user_data_dir,
            color=profile.color,
            driver="existing-session",
            attach_only=True,
        )

    if profile.driver == "control-extension":
        # Wave 6: extension-hosted control. The extension lives inside
        # Chrome (real or managed) and connects to our daemon WS at
        # runtime — no CDP port the daemon needs to know about. Same
        # "all-zero" pattern as existing-session.
        return ResolvedBrowserProfile(
            name=profile_name,
            cdp_port=0,
            cdp_url="",
            cdp_host="",
            cdp_is_loopback=True,
            user_data_dir=profile.user_data_dir,
            color=profile.color,
            driver="control-extension",
            attach_only=True,
        )

    raw_url = (profile.cdp_url or "").strip()
    cdp_port = profile.cdp_port or 0

    if _has_stale_ws_path(raw_url, cdp_port):
        # User has a per-launch ws://host:port/devtools/browser/<uuid> URL;
        # drop the stale UUID path, keep the loopback authority. Per-launch
        # WS URLs become unreachable after Chrome restarts.
        parsed = urlparse(raw_url)
        cdp_host = parsed.hostname or resolved.cdp_host
        cdp_url = f"{resolved.cdp_protocol}://{cdp_host}:{cdp_port}"
    elif raw_url:
        parsed_tuple = _parse_browser_http_url(raw_url)
        if parsed_tuple is None:
            return None
        cdp_host, cdp_port, cdp_url = parsed_tuple
    elif cdp_port:
        cdp_host = resolved.cdp_host
        cdp_url = f"{resolved.cdp_protocol}://{cdp_host}:{cdp_port}"
    else:
        # Neither cdp_url nor cdp_port set on a managed profile is invalid.
        # Surface as None so callers raise a clear "must define" error upstream.
        return None

    driver = profile.driver or "managed"
    attach_only = profile.attach_only if profile.attach_only is not None else resolved.attach_only

    return ResolvedBrowserProfile(
        name=profile_name,
        cdp_port=cdp_port,
        cdp_url=cdp_url,
        cdp_host=cdp_host,
        cdp_is_loopback=_is_loopback_host(cdp_host),
        color=profile.color,
        driver=driver,
        attach_only=attach_only,
        user_data_dir=profile.user_data_dir,
    )

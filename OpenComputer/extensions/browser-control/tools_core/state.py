"""Emulation knobs (offline / headers / credentials / geo / locale / timezone / device).

Distinct from ``server_context.state`` (orchestrator state) — this module
is the per-context Playwright knobs the agent can twiddle. Mostly thin
async wrappers around Playwright's context/page APIs, with a few CDP-only
paths via ``context.new_cdp_session(page)``.

Locale and timezone use CDP overrides (``Emulation.setLocaleOverride`` /
``setTimezoneOverride``) because Playwright doesn't expose a per-page
``setLocale`` after construction. Only one locale override can be in
effect per Chrome target — second call swallows the error silently
(matches Chrome behavior).
"""

from __future__ import annotations

import logging
from typing import Any

_log = logging.getLogger("opencomputer.browser_control.tools_core.state")


# ─── context-wide ────────────────────────────────────────────────────


async def set_offline(context: Any, offline: bool) -> None:
    """Toggle context-wide offline mode. ``navigator.onLine`` reflects this."""
    await context.set_offline(bool(offline))


async def set_extra_http_headers(context: Any, headers: dict[str, str]) -> None:
    if not isinstance(headers, dict):
        raise TypeError("headers must be a dict[str, str]")
    cleaned = {str(k): str(v) for k, v in headers.items()}
    await context.set_extra_http_headers(cleaned)


async def set_http_credentials(
    context: Any,
    *,
    username: str | None = None,
    password: str | None = None,
    clear: bool = False,
) -> None:
    if clear or (not username and not password):
        await context.set_http_credentials(None)  # type: ignore[arg-type]
        return
    await context.set_http_credentials({"username": username or "", "password": password or ""})


async def set_geolocation(
    context: Any,
    *,
    latitude: float | None = None,
    longitude: float | None = None,
    accuracy: float | None = None,
    clear: bool = False,
    grant_for_origin: str | None = None,
) -> None:
    if clear or (latitude is None and longitude is None):
        try:
            await context.clear_permissions()
        except Exception:
            pass
        await context.set_geolocation(None)
        return
    geo: dict[str, float] = {
        "latitude": float(latitude),  # type: ignore[arg-type]
        "longitude": float(longitude),  # type: ignore[arg-type]
    }
    if accuracy is not None:
        geo["accuracy"] = float(accuracy)
    await context.set_geolocation(geo)
    if grant_for_origin:
        try:
            await context.grant_permissions(["geolocation"], origin=grant_for_origin)
        except Exception as exc:
            _log.debug("set_geolocation: grant_permissions failed: %s", exc)


# ─── page-wide ───────────────────────────────────────────────────────


async def emulate_color_scheme(page: Any, scheme: str) -> None:
    if scheme not in ("light", "dark", "no-preference"):
        raise ValueError(f"unknown color scheme: {scheme!r}")
    await page.emulate_media(color_scheme=scheme)


# ─── CDP-mediated overrides ──────────────────────────────────────────


async def _open_cdp(page: Any) -> Any:
    context = page.context
    return await context.new_cdp_session(page)


async def set_locale(page: Any, locale: str) -> None:
    """``Emulation.setLocaleOverride``. Only one override per Chrome target.

    A second call after the first succeeded will throw — Chrome does not
    allow overlapping locale overrides. We surface that as a ``ValueError``
    so the agent can recover (e.g. close + reopen the tab).
    """
    if not locale or not isinstance(locale, str):
        raise ValueError("locale must be a non-empty string")
    cdp = await _open_cdp(page)
    try:
        await cdp.send("Emulation.setLocaleOverride", {"locale": locale})
    except Exception as exc:
        msg = str(exc)
        if "another locale override is already in effect" in msg.lower():
            raise ValueError(
                f"locale override already active on this target ({msg})"
            ) from exc
        raise


async def set_timezone(page: Any, timezone_id: str) -> None:
    if not timezone_id:
        raise ValueError("timezone_id is required")
    cdp = await _open_cdp(page)
    try:
        await cdp.send("Emulation.setTimezoneOverride", {"timezoneId": timezone_id})
    except Exception as exc:
        msg = str(exc)
        if "invalid timezone" in msg.lower():
            raise ValueError(f"invalid timezone {timezone_id!r}") from exc
        raise


async def emulate_device(page: Any, descriptor: dict[str, Any]) -> None:
    """Apply a Playwright device descriptor (`pw.devices["iPhone 13"]`-shaped).

    Sets viewport, user-agent, device-pixel-ratio, and touch-emulation.
    Caller looks up the descriptor; we don't depend on Playwright's
    devices dict here so tests can pass synthetic shapes.
    """
    if not isinstance(descriptor, dict):
        raise TypeError("descriptor must be a dict")
    viewport = descriptor.get("viewport")
    if isinstance(viewport, dict):
        await page.set_viewport_size(
            {"width": int(viewport["width"]), "height": int(viewport["height"])}
        )
    cdp = await _open_cdp(page)
    user_agent = descriptor.get("user_agent") or descriptor.get("userAgent")
    if user_agent:
        await cdp.send("Emulation.setUserAgentOverride", {"userAgent": str(user_agent)})
    if viewport:
        device_metrics: dict[str, Any] = {
            "width": int(viewport["width"]),
            "height": int(viewport["height"]),
            "deviceScaleFactor": float(descriptor.get("device_scale_factor", 1)),
            "mobile": bool(descriptor.get("is_mobile", False)),
        }
        await cdp.send("Emulation.setDeviceMetricsOverride", device_metrics)
    has_touch = bool(descriptor.get("has_touch", False))
    if has_touch:
        await cdp.send(
            "Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5}
        )

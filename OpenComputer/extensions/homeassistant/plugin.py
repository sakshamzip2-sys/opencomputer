"""Home Assistant channel plugin — entry point.

Outbound: REST API (service calls — see adapter docstring).
Inbound: optional WebSocket subscription to ``state_changed`` events
when filter env vars are set. 2026-04-28 follow-up — webhook-driven
inbound (HA → OC webhook) remains an alternative pattern.

Env vars (required):
- ``HOMEASSISTANT_URL`` (e.g. ``http://homeassistant.local:8123``)
- ``HOMEASSISTANT_TOKEN`` (long-lived access token)

Env vars (optional inbound — closed by default):
- ``HASS_WATCH_ALL=true`` — forward EVERY state_changed event
- ``HASS_WATCH_DOMAINS`` — CSV (e.g. ``binary_sensor,sensor``)
- ``HASS_WATCH_ENTITIES`` — CSV (e.g. ``light.front_door``)
- ``HASS_IGNORE_ENTITIES`` — CSV ignore-list
- ``HASS_COOLDOWN_SECONDS`` — per-entity cooldown (default 30)
"""

from __future__ import annotations

import logging
import os

try:
    from adapter import HomeAssistantAdapter  # plugin-loader mode
except ImportError:  # pragma: no cover
    from extensions.homeassistant.adapter import (  # package mode
        HomeAssistantAdapter,
    )

from plugin_sdk.core import Platform

logger = logging.getLogger("opencomputer.ext.homeassistant")


def _csv(env_name: str) -> list[str]:
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        return []
    return [s.strip() for s in raw.split(",") if s.strip()]


def register(api) -> None:  # PluginAPI duck-typed
    # Action tools — Hermes parity (2026-05-01). Registered unconditionally
    # so the model always sees the schema; ``execute()`` returns a
    # structured error when ``HOMEASSISTANT_TOKEN`` is unset.
    try:
        try:
            from action_tools import ALL_TOOLS
        except ImportError:  # pragma: no cover
            from extensions.homeassistant.action_tools import ALL_TOOLS
        for tool_cls in ALL_TOOLS:
            try:
                api.register_tool(tool_cls())
            except Exception as exc:  # noqa: BLE001
                logger.warning("HA action_tool registration failed (%s): %s", tool_cls.__name__, exc)
    except Exception as exc:  # noqa: BLE001
        logger.warning("HA action_tools import failed: %s", exc)

    # Channel adapter — requires URL+TOKEN to actually connect.
    url = os.environ.get("HOMEASSISTANT_URL", "").strip()
    token = os.environ.get("HOMEASSISTANT_TOKEN", "").strip()
    if not url or not token:
        logger.info(
            "homeassistant plugin: action tools registered, channel skipped "
            "(HOMEASSISTANT_URL or HOMEASSISTANT_TOKEN unset)"
        )
        return
    config = {
        "url": url,
        "token": token,
        # Inbound — all default empty/False so legacy outbound-only
        # deployments are unaffected.
        "watch_domains": _csv("HASS_WATCH_DOMAINS"),
        "watch_entities": _csv("HASS_WATCH_ENTITIES"),
        "ignore_entities": _csv("HASS_IGNORE_ENTITIES"),
        "watch_all": os.environ.get("HASS_WATCH_ALL", "").lower() == "true",
        "cooldown_seconds": int(os.environ.get("HASS_COOLDOWN_SECONDS", "30")),
    }
    adapter = HomeAssistantAdapter(config=config)
    api.register_channel(Platform.HOMEASSISTANT.value, adapter)
    logger.info("homeassistant plugin: registered for %s", url)

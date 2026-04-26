"""Browser-bridge plugin — wires the adapter into OpenComputer's gateway."""
from __future__ import annotations

import logging
from typing import Any

from plugin_sdk import PluginManifest

from extensions.browser_bridge.adapter import BrowserBridgeAdapter, generate_token

_log = logging.getLogger("extensions.browser_bridge.plugin")


def register(api: Any) -> PluginManifest:
    """Register the browser-bridge plugin with the host."""
    return PluginManifest(
        id="browser-bridge",
        name="Browser Bridge",
        version="0.1.0",
        description=(
            "Receives tab activity from the OpenComputer browser extension "
            "and fans events into the F2 SignalEvent bus."
        ),
        kind="tools",
    )


__all__ = ["register", "BrowserBridgeAdapter", "generate_token"]

"""Browser-bridge plugin — wires the adapter into OpenComputer's gateway."""
from __future__ import annotations

import logging
from typing import Any

# Plugin-loader mode: sibling modules are importable by plain name (the
# loader puts the plugin's directory on sys.path and imports plugin.py
# directly). Package mode (e.g. tests using the conftest alias fixture):
# falls through to ``extensions.browser_bridge.adapter``.
try:
    from adapter import BrowserBridgeAdapter, generate_token  # plugin-loader mode
except ImportError:  # pragma: no cover - exercised via conftest alias fixture
    from extensions.browser_bridge.adapter import (  # type: ignore[no-redef]
        BrowserBridgeAdapter,
        generate_token,
    )

from plugin_sdk import PluginManifest

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
        kind="tool",
    )


__all__ = ["register", "BrowserBridgeAdapter", "generate_token"]

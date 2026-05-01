"""Browser-control plugin — Playwright-based automation."""
from __future__ import annotations

import logging

_log = logging.getLogger("opencomputer.browser_control.plugin")


def register(api) -> None:  # noqa: ANN001
    """Register all browser tools (5 base + 6 Hermes-parity)."""
    try:
        from extensions.browser_control.tools import ALL_TOOLS
        for tool_cls in ALL_TOOLS:
            try:
                api.register_tool(tool_cls())
            except Exception as exc:  # noqa: BLE001
                _log.warning("Failed to register %s: %s", tool_cls.__name__, exc)
    except ImportError as exc:
        _log.warning("browser-control tools not loadable: %s", exc)

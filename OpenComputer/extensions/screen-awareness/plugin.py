"""Plugin entry. Wiring is no-op until the sensor + injection provider
ship in later tasks. Plugin is registered but inert by default.
"""
from __future__ import annotations

import logging

_log = logging.getLogger("opencomputer.screen_awareness.plugin")


def register(api) -> None:  # noqa: ANN001
    """Plugin entry. Sensor wiring happens in Task 10."""
    _log.debug(
        "screen-awareness plugin registered (sensor wiring deferred to Task 10)"
    )

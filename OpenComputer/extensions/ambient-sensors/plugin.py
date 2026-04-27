"""Ambient sensors plugin — Phase 1: foreground-app observation.

The daemon does NOT auto-start. The user opts in via ``oc ambient on``;
the daemon launches at gateway boot when state.enabled is True (T8 wires
this) or via ``oc ambient daemon`` standalone.
"""

from __future__ import annotations

import logging

_log = logging.getLogger("opencomputer.ambient.plugin")


def register(api) -> None:  # noqa: ANN001
    """Plugin entry. Daemon lifecycle is gateway-managed (T8) or standalone."""
    _log.debug("ambient-sensors plugin registered (daemon starts via gateway/CLI)")

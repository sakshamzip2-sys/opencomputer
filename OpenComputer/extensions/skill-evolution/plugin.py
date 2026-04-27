"""Skill-evolution plugin — Phase 1: auto-extract reusable skills from successful sessions.

The subscriber does NOT auto-start. The user opts in via
``oc skills evolution on`` and the subscriber launches at gateway boot
(or via ``oc skills evolution daemon`` standalone if we add one in Phase 2).
"""

from __future__ import annotations

import logging

_log = logging.getLogger("opencomputer.skill_evolution.plugin")


def register(api) -> None:  # noqa: ANN001
    """Plugin entry. Daemon lifecycle is gateway-managed (T8) or CLI-driven."""
    _log.debug(
        "skill-evolution plugin registered (subscriber starts via gateway/CLI)"
    )

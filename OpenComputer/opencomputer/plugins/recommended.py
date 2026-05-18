"""Core plugin trio — the always-on recommended plugin set.

The trio — ``coding-harness``, ``memory-honcho``, ``dev-tools`` — ships
OpenComputer's agent capabilities (coding tools, three-pillar memory,
dev-tools). Recipe A.2 of
``docs/refs/2026-05-17-coding-harness-and-orchestration-gaps.md`` makes
the trio always-on: :func:`apply_core_defaults` unions it into every
profile's resolved plugin filter, so a profile with a concrete
``plugins.enabled`` list no longer silently loses the agent's core
tooling. ``plugins.disabled`` is the explicit opt-out.

This supersedes the install-but-dark startup WARN (PR #644): with the
trio always-on, ``coding-harness`` can only be absent when the user
explicitly disables it, so there is nothing left to nudge about.
"""

from __future__ import annotations

from typing import Literal

# Single source of truth for the core plugin trio. ``coding-harness``
# MUST stay first — the setup wizard and profile resolver treat it as
# the primary agent plugin. Re-exported by
# ``cli_setup.section_handlers.tools`` for the wizard's recommended set.
RECOMMENDED_PLUGINS: tuple[str, ...] = ("coding-harness", "memory-honcho", "dev-tools")


def apply_core_defaults(
    *,
    enabled: frozenset[str] | Literal["*"],
    disabled: frozenset[str],
) -> frozenset[str] | Literal["*"]:
    """Union the always-on core plugin trio into a resolved plugin filter.

    ``enabled`` is whatever the profile resolver produced:

    * ``"*"`` (wildcard) — already loads everything; returned unchanged.
      ``disabled`` does NOT carve a wildcard — to exclude a plugin, use
      a concrete ``plugins.enabled`` list.
    * a concrete ``frozenset`` — the trio is unioned in, then
      ``disabled`` is subtracted. An explicit opt-out therefore wins
      over both the default union and an explicit ``enabled`` entry.
    """
    if enabled == "*":
        return "*"
    concrete = enabled if isinstance(enabled, frozenset) else frozenset(enabled)
    return (concrete | frozenset(RECOMMENDED_PLUGINS)) - disabled


__all__ = ["RECOMMENDED_PLUGINS", "apply_core_defaults"]

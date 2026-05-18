"""Recommended-plugins constant + coding-harness dark-state WARN.

Recipe A (M1.0) from
``docs/refs/2026-05-17-coding-harness-and-orchestration-gaps.md``.

The coding-harness plugin ships OpenComputer's whole coding-agent
surface — ``Edit`` / ``MultiEdit`` / ``TodoWrite``, plan-mode,
accept-edits-mode, checkpoints, the ``/plan`` ``/checkpoint`` ``/diff``
slash commands. A profile whose ``plugins.enabled`` list excludes it
(common: profiles written by the older setup wizard, by ``oc plugin
enable``, or hand-edited) leaves the agent with only ``Read`` / ``Write``
/ ``Bash`` and no negative signal — the LLM never sees ``Edit`` in its
schema, so the demand-tracker can't fire either.

This module is the M1.0 fix: a single-line nudge at ``oc chat`` startup
when the harness is installed but dark. It does NOT change plugin-load
semantics (that was the audit's rejected Recipe-A.1 auto-migration).

Suppression — for sandbox / audit profiles that intentionally run an
empty plugin set:

* ``OPENCOMPUTER_NO_HARNESS_WARN`` env var set to any non-empty value;
* ``plugins.suppress_harness_warning: true`` in the profile's
  ``profile.yaml``.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path

import yaml
from rich.console import Console

logger = logging.getLogger("opencomputer.plugins.recommended")

# Single source of truth for the plugin trio the setup wizard seeds and
# the chat-startup WARN nudges toward. ``coding-harness`` MUST stay first
# — the WARN message and ``is_harness_dark`` reference it by this name.
RECOMMENDED_PLUGINS: tuple[str, ...] = ("coding-harness", "memory-honcho", "dev-tools")

_HARNESS_ID = "coding-harness"
_SUPPRESS_ENV_VAR = "OPENCOMPUTER_NO_HARNESS_WARN"
_SUPPRESS_PROFILE_KEY = "suppress_harness_warning"


def is_harness_dark(
    *,
    enabled_ids: frozenset[str] | str | None,
    installed_plugin_ids: frozenset[str],
) -> bool:
    """Return True when coding-harness is installed but the active
    plugin filter excludes it.

    ``enabled_ids`` is whatever ``cli._resolve_plugin_filter`` produced:

    * ``None`` — malformed profile config / missing preset → load-all;
    * ``"*"`` — explicit wildcard → load-all;
    * a concrete ``frozenset`` — only these ids load.

    Only the concrete-frozenset case can be dark. A wildcard means the
    harness loads on its own; an uninstalled harness has no fix to
    suggest; a harness already in the list is not dark.
    """
    if enabled_ids is None or enabled_ids == "*":
        return False
    if not isinstance(enabled_ids, (frozenset, set)):
        # Defensive — the resolver contract is frozenset | "*" | None,
        # but an unexpected shape must not produce a false WARN.
        return False
    if _HARNESS_ID not in installed_plugin_ids:
        return False
    return _HARNESS_ID not in enabled_ids


def _suppressed_by_profile(profile_yaml: Path) -> bool:
    """Read ``plugins.suppress_harness_warning`` from a profile.yaml.

    Tolerant by contract: a missing / unreadable / malformed file
    returns False (no suppression configured) rather than raising —
    chat startup must never crash on a cosmetic nag. Mirrors the
    lenient parse in ``cli_profile._read_enabled_plugin_ids``.
    """
    if not profile_yaml.exists():
        return False
    try:
        data = yaml.safe_load(profile_yaml.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return False
    if not isinstance(data, dict):
        return False
    plugins_block = data.get("plugins")
    if not isinstance(plugins_block, dict):
        return False
    return bool(plugins_block.get(_SUPPRESS_PROFILE_KEY, False))


def maybe_warn_harness_dark(
    *,
    enabled_ids: frozenset[str] | str | None,
    installed_plugin_ids: frozenset[str],
    profile_yaml: Path,
    console: Console,
    env: Mapping[str, str],
) -> bool:
    """Print a one-line nudge when coding-harness is installed-but-dark.

    Returns True iff the WARN was printed. Silent (returns False) when
    the harness is loaded or uninstalled, the filter is a wildcard, the
    ``OPENCOMPUTER_NO_HARNESS_WARN`` env var holds a non-empty value, or
    the profile sets ``plugins.suppress_harness_warning: true``.

    ``env`` is injected (rather than read from ``os.environ`` directly)
    so callers and tests stay explicit about the environment in scope.
    """
    if not is_harness_dark(
        enabled_ids=enabled_ids, installed_plugin_ids=installed_plugin_ids
    ):
        return False
    if env.get(_SUPPRESS_ENV_VAR):
        return False
    if _suppressed_by_profile(profile_yaml):
        return False
    # soft_wrap keeps this a single logical line regardless of terminal
    # width — the doc specifies a one-line WARN, and narrow terminals
    # should reflow visually rather than gain a hard newline.
    console.print(
        "[yellow]![/yellow] coding-harness is installed but not enabled — "
        "the agent has no Edit/plan-mode/checkpoints this session. "
        "Fix: [bold]oc plugin enable coding-harness[/bold]  ·  "
        f"silence: {_SUPPRESS_ENV_VAR}=1",
        soft_wrap=True,
    )
    return True


__all__ = [
    "RECOMMENDED_PLUGINS",
    "is_harness_dark",
    "maybe_warn_harness_dark",
]

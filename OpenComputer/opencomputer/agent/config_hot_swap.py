"""Config hot-swap for profile handoff — §9.4 of
``docs/plans/profile-handoff-investigation.md``.

A ``Config`` is a frozen dataclass with ~20 top-level fields. Some
are read at component construction time (e.g. ``loop.max_iterations``
is captured by the AgentLoop instance) and changing them mid-loop is
either useless or actively unsafe. Others (e.g. ``model.model``,
``memory.*``, ``compaction.*``) are read on every turn and CAN be
swapped live.

This module enumerates the **safe / hot-swappable** fields as an
explicit allowlist. Unknown / un-allowlisted fields → restart-required.
Allowlist-not-denylist is deliberate: when a new config field is
introduced, the safe default is "restart-required" until someone
explicitly classifies it as hot-swappable. This is the opposite of
the easy mistake (auto-hot-swap-everything-then-debug-the-fires).

The hot-swap is **atomic from the AgentLoop's perspective** — either
the whole new config replaces ``self.config``, or nothing changes.
There is no in-between half-applied state.
"""
from __future__ import annotations

import dataclasses
import logging
from pathlib import Path
from typing import Any

from opencomputer.agent.config import Config

_log = logging.getLogger(__name__)


def _load_profile_config(profile_root: Path) -> Config:
    """Load ``<profile_root>/config.yaml`` rooted at the new profile.

    Combines :func:`opencomputer.agent.config_store.load_config` (YAML
    parser) with :func:`plugin_sdk.profile_context.set_profile` so any
    path-field-factories under that context resolve to the new
    profile's home.

    Missing config.yaml is fine — defaults are used (still rooted at
    the new profile's home).
    """
    from opencomputer.agent.config_store import load_config
    from plugin_sdk.profile_context import set_profile

    cfg_path = profile_root / "config.yaml"
    with set_profile(profile_root):
        if cfg_path.exists():
            return load_config(cfg_path)
        # Fall through to defaults (still set_profile-rooted).
        from opencomputer.agent.config import default_config

        return default_config()


# Top-level Config fields that may be hot-swapped on profile handoff.
# Add a field here ONLY after verifying:
#   1. It is read on every turn (not captured at __init__ time).
#   2. Mid-conversation changes do not break in-flight state.
#   3. There is a test exercising the swap.
#
# Restart-required (intentionally NOT in this set):
#   - "loop": max_iterations / iteration timeouts captured at boot
#   - "hooks" + "prompt_hooks" + "agent_hooks" + "http_hooks":
#     hook engine + subscriptions loaded once
#   - "mcp": handled separately by MCPManager.diff_cycle (§9.5)
#   - "tools": allowed_tools captured at AgentLoop __init__
#   - "session": SessionDB path captured at __init__ (handled by §9.2)
#   - "system_control", "cron", "worktree", "checkpoints": all start-time
HOT_SWAPPABLE_TOP_LEVEL_FIELDS: frozenset[str] = frozenset(
    {
        # ModelConfig — provider/model/sampling can swap; the provider
        # rebind handler (§9.7) rebuilds the client to honor it.
        "model",
        # MemoryConfig — declarative_path / user_path / soul_path are
        # already re-pointed by ``memory.rebind_to_profile``; this just
        # keeps Config in sync so subsequent reads agree.
        "memory",
        # GatewayConfig — photo-burst window etc. are read per-message.
        "gateway",
        # DeepeningConfig — Layer-3 extractor + cost controls; read per
        # extraction call, not bound to AgentLoop.
        "deepening",
        # Per-model context window overrides — consulted per-turn.
        "model_context_overrides",
        # Credential pool strategies — looked up per-call.
        "credential_pool_strategies",
        # Custom providers map — resolver consults on every model swap.
        "custom_providers",
    }
)


@dataclasses.dataclass(frozen=True, slots=True)
class HotSwapResult:
    """Outcome of a single config hot-swap invocation."""

    applied: tuple[str, ...]
    """Top-level field names successfully copied from the new config."""

    skipped_restart_required: tuple[str, ...]
    """Top-level fields that changed in the new config but are NOT
    allowlisted as hot-swappable (would need a process restart)."""

    error: str | None
    """Human-readable error string, or ``None`` on success. Errors do
    NOT raise — the rebind registry catches and logs them but partial
    success here is by design (best-effort)."""


def compute_field_deltas(old_config: Config, new_config: Config) -> dict[str, bool]:
    """Compare top-level fields. Returns ``{field_name: changed}``.

    Used to drive the WARN message that lists which restart-required
    fields differ — surfacing the cost of the partial swap to the user.
    """
    deltas: dict[str, bool] = {}
    for field in dataclasses.fields(old_config):
        old_val = getattr(old_config, field.name, None)
        new_val = getattr(new_config, field.name, None)
        # Frozen dataclasses compare by value via __eq__ generated by
        # @dataclass; for nested mutable types (tuple, dict-like) this
        # still works because Config and all nested types are frozen.
        deltas[field.name] = old_val != new_val
    return deltas


def hot_swap_config(
    old_config: Config,
    new_profile_root: Path,
) -> tuple[Config, HotSwapResult]:
    """Build a new Config that applies allowlisted hot-swappable fields.

    Args:
        old_config: The currently-active Config — preserved for any
            field NOT in ``HOT_SWAPPABLE_TOP_LEVEL_FIELDS``.
        new_profile_root: Profile root (NOT ``home/`` subdir) whose
            ``config.yaml`` should be loaded as the source of new
            field values.

    Returns:
        Tuple of ``(merged_config, HotSwapResult)``. On error
        ``merged_config`` is the original ``old_config`` unchanged.

    Raises:
        TypeError: bad argument types.
    """
    if not isinstance(old_config, Config):
        raise TypeError(
            f"old_config must be Config, got {type(old_config).__name__}"
        )
    if not isinstance(new_profile_root, Path):
        raise TypeError(
            f"new_profile_root must be Path, got {type(new_profile_root).__name__}"
        )

    try:
        new_config = _load_profile_config(new_profile_root)
    except Exception as exc:  # noqa: BLE001 — partial swap is fine
        return old_config, HotSwapResult(
            applied=(),
            skipped_restart_required=(),
            error=f"failed to load new profile config: {exc}",
        )

    deltas = compute_field_deltas(old_config, new_config)

    apply: dict[str, Any] = {}
    skip: list[str] = []
    for field_name, changed in deltas.items():
        if not changed:
            continue
        if field_name in HOT_SWAPPABLE_TOP_LEVEL_FIELDS:
            apply[field_name] = getattr(new_config, field_name)
        else:
            skip.append(field_name)

    if not apply:
        # Nothing actually changed in hot-swappable space.
        return old_config, HotSwapResult(
            applied=(),
            skipped_restart_required=tuple(sorted(skip)),
            error=None,
        )

    try:
        merged = dataclasses.replace(old_config, **apply)
    except Exception as exc:  # noqa: BLE001
        return old_config, HotSwapResult(
            applied=(),
            skipped_restart_required=tuple(sorted(skip)),
            error=f"dataclasses.replace failed: {exc}",
        )

    if skip:
        _log.warning(
            "config hot-swap: %d field(s) require restart to apply: %s "
            "(applied %d hot-swappable: %s)",
            len(skip),
            ", ".join(sorted(skip)),
            len(apply),
            ", ".join(sorted(apply.keys())),
        )
    else:
        _log.info(
            "config hot-swap: applied %d field(s): %s",
            len(apply), ", ".join(sorted(apply.keys())),
        )

    return merged, HotSwapResult(
        applied=tuple(sorted(apply.keys())),
        skipped_restart_required=tuple(sorted(skip)),
        error=None,
    )


__all__ = [
    "HOT_SWAPPABLE_TOP_LEVEL_FIELDS",
    "HotSwapResult",
    "compute_field_deltas",
    "hot_swap_config",
]

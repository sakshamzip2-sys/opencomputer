"""Core trio always-on — coding-harness / memory-honcho / dev-tools load
for every profile unless explicitly disabled.

Recipe A.2 of ``docs/refs/2026-05-17-coding-harness-and-orchestration-gaps.md``
(the deferred "defaults-on"). A profile whose ``plugins.enabled`` is a
concrete list previously dropped the core agent plugins entirely; now the
trio is force-unioned into the resolved filter, and ``plugins.disabled``
is the explicit opt-out.
"""

from __future__ import annotations

import pytest

from opencomputer.agent.profile_config import (
    ProfileConfig,
    ProfileConfigError,
    resolve_enabled_plugins,
    validate_profile_config_dict,
)
from opencomputer.plugins.recommended import RECOMMENDED_PLUGINS, apply_core_defaults

_TRIO = frozenset(RECOMMENDED_PLUGINS)


# ──────────────────────────────────────────────────────────────────────
# apply_core_defaults — the pure union/subtract helper
# ──────────────────────────────────────────────────────────────────────


def test_wildcard_stays_wildcard() -> None:
    """``"*"`` already loads everything — must not be rewritten to a
    concrete set (that would silently NARROW the filter)."""
    assert apply_core_defaults(enabled="*", disabled=frozenset()) == "*"


def test_wildcard_unaffected_by_disabled() -> None:
    """``plugins.disabled`` does not carve the wildcard — documented
    limitation: to exclude a plugin, use a concrete list."""
    assert apply_core_defaults(enabled="*", disabled=frozenset({"coding-harness"})) == "*"


def test_concrete_list_gains_the_trio() -> None:
    result = apply_core_defaults(
        enabled=frozenset({"telegram"}), disabled=frozenset()
    )
    assert result == frozenset({"telegram"}) | _TRIO


def test_empty_list_resolves_to_exactly_the_trio() -> None:
    """``plugins.enabled: []`` used to mean 'load nothing' — now it
    means 'just the core trio'."""
    assert apply_core_defaults(enabled=frozenset(), disabled=frozenset()) == _TRIO


def test_disabled_removes_a_trio_member() -> None:
    """Explicit opt-out: a trio member named in ``disabled`` is not
    force-added."""
    result = apply_core_defaults(
        enabled=frozenset({"telegram"}),
        disabled=frozenset({"coding-harness"}),
    )
    assert isinstance(result, frozenset)
    assert "coding-harness" not in result
    assert {"telegram", "memory-honcho", "dev-tools"} <= result


def test_disabled_wins_over_explicit_enable() -> None:
    """A plugin in BOTH lists is a contradiction — ``disabled`` wins
    (an explicit 'no' beats an explicit 'yes')."""
    result = apply_core_defaults(
        enabled=frozenset({"coding-harness", "telegram"}),
        disabled=frozenset({"coding-harness"}),
    )
    assert "coding-harness" not in result
    assert "telegram" in result


def test_disabled_subtracts_non_trio_too() -> None:
    """``disabled`` is a plain subtraction from concrete lists, not
    trio-only."""
    result = apply_core_defaults(
        enabled=frozenset({"telegram", "discord"}),
        disabled=frozenset({"discord"}),
    )
    assert "discord" not in result
    assert "telegram" in result


# ──────────────────────────────────────────────────────────────────────
# resolve_enabled_plugins — end-to-end through the profile resolver
# ──────────────────────────────────────────────────────────────────────


def test_resolve_concrete_list_includes_trio() -> None:
    cfg = ProfileConfig(enabled_plugins=frozenset({"telegram"}))
    resolved = resolve_enabled_plugins(cfg)
    assert isinstance(resolved.enabled, frozenset)
    assert resolved.enabled >= _TRIO
    assert "telegram" in resolved.enabled


def test_resolve_empty_list_is_the_trio() -> None:
    cfg = ProfileConfig(enabled_plugins=frozenset())
    resolved = resolve_enabled_plugins(cfg)
    assert resolved.enabled == _TRIO


def test_resolve_wildcard_stays_wildcard() -> None:
    cfg = ProfileConfig(enabled_plugins="*")
    resolved = resolve_enabled_plugins(cfg)
    assert resolved.enabled == "*"


def test_resolve_honors_disabled() -> None:
    cfg = ProfileConfig(
        enabled_plugins=frozenset({"telegram"}),
        disabled_plugins=frozenset({"coding-harness"}),
    )
    resolved = resolve_enabled_plugins(cfg)
    assert resolved.enabled == frozenset({"telegram", "memory-honcho", "dev-tools"})


def test_resolve_source_trail_mentions_core_defaults() -> None:
    """The human-readable trail (used by `oc doctor` + loader logs)
    records that the trio was unioned in."""
    cfg = ProfileConfig(enabled_plugins=frozenset({"telegram"}))
    resolved = resolve_enabled_plugins(cfg)
    assert "core" in resolved.source.lower()


# ──────────────────────────────────────────────────────────────────────
# validate_profile_config_dict — parsing plugins.disabled
# ──────────────────────────────────────────────────────────────────────


def test_validate_parses_disabled_list() -> None:
    cfg = validate_profile_config_dict(
        {"plugins": {"enabled": ["telegram"], "disabled": ["coding-harness"]}}
    )
    assert cfg.disabled_plugins == frozenset({"coding-harness"})
    assert cfg.enabled_plugins == frozenset({"telegram"})


def test_validate_disabled_absent_is_empty() -> None:
    cfg = validate_profile_config_dict({"plugins": {"enabled": ["telegram"]}})
    assert cfg.disabled_plugins == frozenset()


def test_validate_disabled_must_be_a_list() -> None:
    with pytest.raises(ProfileConfigError, match="disabled"):
        validate_profile_config_dict({"plugins": {"disabled": "coding-harness"}})


def test_validate_disabled_must_be_strings() -> None:
    with pytest.raises(ProfileConfigError, match="disabled"):
        validate_profile_config_dict({"plugins": {"disabled": [123]}})


def test_validate_disabled_alongside_wildcard() -> None:
    """``disabled`` is an independent axis — allowed with ``enabled: '*'``
    even though it has no effect on a wildcard."""
    cfg = validate_profile_config_dict(
        {"plugins": {"enabled": "*", "disabled": ["coding-harness"]}}
    )
    assert cfg.enabled_plugins == "*"
    assert cfg.disabled_plugins == frozenset({"coding-harness"})


# ──────────────────────────────────────────────────────────────────────
# RECOMMENDED_PLUGINS — single source of truth
# ──────────────────────────────────────────────────────────────────────


def test_recommended_plugins_pins_coding_harness_first() -> None:
    """coding-harness MUST be index 0 — the wizard and profile resolver
    treat it as the primary agent plugin."""
    assert RECOMMENDED_PLUGINS[0] == "coding-harness"
    assert set(RECOMMENDED_PLUGINS) == {
        "coding-harness",
        "memory-honcho",
        "dev-tools",
    }
    assert isinstance(RECOMMENDED_PLUGINS, tuple)


def test_setup_wizard_reexports_canonical_tuple() -> None:
    """The setup wizard's ``_RECOMMENDED_PLUGINS`` is the SAME object —
    one source of truth for the trio across wizard + resolver."""
    from opencomputer.cli_setup.section_handlers.tools import _RECOMMENDED_PLUGINS

    assert _RECOMMENDED_PLUGINS is RECOMMENDED_PLUGINS

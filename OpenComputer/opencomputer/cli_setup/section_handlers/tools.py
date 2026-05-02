"""Tools / plugin-preset section (S4).

Modeled after Hermes's setup_tools (hermes_cli/setup.py:2526).
Independently re-implemented (no code copied).

Single 2-option radiolist:
  1. Apply recommended preset — enables (or merges-in) the canonical
     OC starter plugins: coding-harness, memory-honcho, dev-tools.
  2. Skip — keep current plugin set untouched.

Apply path is additive: if the user already enabled additional
plugins, those are preserved. Duplicates are deduplicated.

Per-plugin granular toggles are deferred — users can edit
~/.opencomputer/config.yaml or run `oc plugins` for fine-grained
control. This section's job is the fast-path "enable the obvious
defaults" button.
"""
from __future__ import annotations

from opencomputer.cli_setup.sections import SectionResult, WizardCtx
from opencomputer.cli_ui.menu import Choice, radiolist

# Canonical OC starter set. Mirrors README's "minimum useful set".
_RECOMMENDED_PLUGINS: tuple[str, ...] = (
    "coding-harness",
    "memory-honcho",
    "dev-tools",
)


def _apply_recommended(ctx: WizardCtx) -> list[str]:
    """Merge recommended plugins into config.plugins.enabled. Returns
    the final enabled list."""
    plugins_block = ctx.config.setdefault("plugins", {})
    enabled = list(plugins_block.setdefault("enabled", []))
    for name in _RECOMMENDED_PLUGINS:
        if name not in enabled:
            enabled.append(name)
    plugins_block["enabled"] = enabled
    return enabled


def _print_summary(enabled: list[str]) -> None:
    print("  ✓ Applied recommended plugin preset:")
    for name in _RECOMMENDED_PLUGINS:
        print(f"      • {name}")
    extras = [p for p in enabled if p not in _RECOMMENDED_PLUGINS]
    if extras:
        print(f"  Existing plugins kept: {', '.join(extras)}")
    print("  Run `oc plugins` later to toggle individual plugins.")


def run_tools_section(ctx: WizardCtx) -> SectionResult:
    choices = [
        Choice("Apply recommended plugin preset", "apply"),
        Choice("Skip — keep current plugin set", "skip"),
    ]
    idx = radiolist(
        "Configure tools / plugins?",
        choices, default=0,
        description="Recommended preset: coding-harness + memory-honcho "
                     "+ dev-tools. Additive — your existing plugins are kept.",
    )
    if idx == 1:
        return SectionResult.SKIPPED_FRESH

    enabled = _apply_recommended(ctx)
    _print_summary(enabled)
    return SectionResult.CONFIGURED

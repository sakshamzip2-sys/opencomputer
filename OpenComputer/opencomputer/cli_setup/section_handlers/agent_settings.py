"""Agent-settings section handler (S1).

Modeled after Hermes's "Applied recommended defaults" pattern in
hermes_cli/setup.py::setup_agent_settings — independently
re-implemented (no code copied).

Single 2-option radiolist:
  1. Apply recommended defaults — writes known-good values to config.loop.*
     and prints a summary of what changed.
  2. Skip — keeping current — returns SKIPPED_FRESH untouched.

Per-field customization (max_iterations slider, custom timeout) is a
follow-up. This section's job is the fast-path "I trust the defaults"
button. Users who want to tweak edit ~/.opencomputer/config.yaml or
re-run the wizard.
"""
from __future__ import annotations

from opencomputer.cli_setup.sections import SectionResult, WizardCtx
from opencomputer.cli_ui.menu import Choice, radiolist

# Recommended defaults. Mirrors Hermes screenshot 5 where appropriate;
# otherwise uses OC's own LoopConfig defaults from
# opencomputer/agent/config.py.
_RECOMMENDED: dict[str, object] = {
    "max_iterations": 90,
    "parallel_tools": True,
    "inactivity_timeout_s": 300,    # 5 min
    "iteration_timeout_s": 1800,    # 30 min
    "delegation_max_iterations": 50,
    "max_delegation_depth": 2,
    "context_engine": "compressor",
}


def is_agent_settings_configured(ctx: WizardCtx) -> bool:
    """True if the loop section has any user-set values."""
    loop = ctx.config.get("loop") or {}
    return bool(loop)


def _apply_recommended(ctx: WizardCtx) -> None:
    """Overwrite config.loop with the recommended defaults."""
    ctx.config.setdefault("loop", {})
    ctx.config["loop"].update(_RECOMMENDED)


def _print_summary() -> None:
    print(
        "  ✓ Applied recommended defaults:\n"
        f"      Max iterations: {_RECOMMENDED['max_iterations']}\n"
        f"      Parallel tools: {_RECOMMENDED['parallel_tools']}\n"
        f"      Inactivity timeout: {_RECOMMENDED['inactivity_timeout_s']}s "
        "(5 min)\n"
        f"      Iteration timeout: {_RECOMMENDED['iteration_timeout_s']}s "
        "(30 min)\n"
        "  Edit ~/.opencomputer/config.yaml later to customize."
    )


def run_agent_settings_section(ctx: WizardCtx) -> SectionResult:
    choices = [
        Choice("Apply recommended defaults", "apply"),
        Choice("Skip — keep current", "skip"),
    ]
    idx = radiolist(
        "Configure agent loop settings?",
        choices, default=0,
        description="Recommended defaults: 90 iterations, parallel tools on, "
                     "5-min inactivity / 30-min iteration timeouts.",
    )
    if idx == 1:
        return SectionResult.SKIPPED_FRESH

    _apply_recommended(ctx)
    _print_summary()
    return SectionResult.CONFIGURED

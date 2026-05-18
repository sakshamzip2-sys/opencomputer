"""Agent-settings setup section."""
from __future__ import annotations

from opencomputer.cli_setup.sections import SectionResult, WizardCtx
from opencomputer.cli_ui.menu import Choice, radiolist

# Mirrors the live LoopConfig defaults (opencomputer/agent/config.py).
# Keep in sync when those caps change — these values get pinned into the
# user's config.yaml, so stale entries here become stale user configs.
_RECOMMENDED: dict[str, object] = {
    "max_iterations": 100,
    "parallel_tools": True,
    "inactivity_timeout_s": 1800,
    "iteration_timeout_s": 7200,
    "delegation_max_iterations": 100,
    "max_delegation_depth": 4,
    "context_engine": "compressor",
}


def is_agent_settings_configured(ctx: WizardCtx) -> bool:
    return bool(ctx.config.get("loop"))


def _apply_recommended(ctx: WizardCtx) -> None:
    ctx.config.setdefault("loop", {}).update(_RECOMMENDED)
    ctx.config.setdefault("display", {})["tool_progress"] = "all"
    ctx.config.setdefault("context", {})["compression_threshold"] = 0.5
    ctx.config.setdefault("session_reset", {}).update({
        "mode": "inactivity_daily",
        "inactivity_timeout_minutes": 1440,
        "daily_reset_hour": 4,
    })


def _prompt_int(label: str, default: int) -> int:
    try:
        raw = input(f"{label} [{default}]: ").strip()
    except (EOFError, OSError):
        return default
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"  Invalid number; keeping {default}.")
        return default


def _prompt_float(label: str, default: float) -> float:
    try:
        raw = input(f"{label} [{default}]: ").strip()
    except (EOFError, OSError):
        return default
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        print(f"  Invalid number; keeping {default}.")
        return default


def _customize(ctx: WizardCtx) -> None:
    loop = ctx.config.setdefault("loop", {})
    loop["max_iterations"] = _prompt_int(
        "Max iterations",
        int(loop.get("max_iterations") or _RECOMMENDED["max_iterations"]),
    )
    print("  ✓ Max iterations set to", loop["max_iterations"])

    progress_choices = [
        Choice("off - Silent, just the final response", "off"),
        Choice("new - Show tool name only when it changes", "new"),
        Choice("all - Show every tool call with a short preview", "all"),
        Choice("verbose - Full args, results, and debug logs", "verbose"),
    ]
    display = ctx.config.setdefault("display", {})
    progress_idx = radiolist("Tool progress mode:", progress_choices, default=2)
    display["tool_progress"] = progress_choices[progress_idx].value
    print("  ✓ Tool progress set to", display["tool_progress"])

    context = ctx.config.setdefault("context", {})
    context["compression_threshold"] = _prompt_float(
        "Compression threshold (0.5-0.95)",
        float(context.get("compression_threshold") or 0.5),
    )
    print("  ✓ Context compression threshold set to", context["compression_threshold"])

    reset_choices = [
        Choice("Inactivity + daily reset (recommended)", "inactivity_daily"),
        Choice("Inactivity only", "inactivity"),
        Choice("Daily only", "daily"),
        Choice("Never auto-reset", "never"),
        Choice("Keep current settings", "keep"),
    ]
    reset_idx = radiolist("Session reset mode:", reset_choices, default=0)
    if reset_choices[reset_idx].value != "keep":
        reset = ctx.config.setdefault("session_reset", {})
        reset["mode"] = reset_choices[reset_idx].value
        reset["inactivity_timeout_minutes"] = _prompt_int(
            "Inactivity timeout (minutes)", 1440
        )


def run_agent_settings_section(ctx: WizardCtx) -> SectionResult:
    choices = [
        Choice("Apply recommended defaults", "apply"),
        Choice("Skip - keep current", "skip"),
        Choice("Customize agent settings", "customize"),
    ]
    idx = radiolist(
        "Configure agent settings?",
        choices,
        default=0,
        description="Recommended: 100 iterations, all tool progress, compression at 0.5.",
    )
    action = choices[idx].value
    if action == "skip":
        return SectionResult.SKIPPED_FRESH
    if action == "apply":
        _apply_recommended(ctx)
        print("  ✓ Applied recommended defaults:")
        print("      Max iterations: 100")
        print("      Tool progress: all")
        print("      Inactivity timeout: 1800s (30 min)")
        print("      Compression threshold: 0.50")
        print("      Session reset: inactivity + daily")
        return SectionResult.CONFIGURED

    _customize(ctx)
    return SectionResult.CONFIGURED

"""Optional vision and image-analysis provider section."""
from __future__ import annotations

from opencomputer.cli_setup.sections import SectionResult, WizardCtx
from opencomputer.cli_ui.menu import Choice, radiolist


def is_vision_provider_configured(ctx: WizardCtx) -> bool:
    vision = ctx.config.get("vision") or {}
    return bool(vision.get("provider"))


def run_vision_provider_section(ctx: WizardCtx) -> SectionResult:
    choices = [
        Choice("OpenRouter - uses Gemini", "openrouter"),
        Choice("OpenAI-compatible endpoint - base URL, API key, and vision model", "custom"),
        Choice("Skip - configure later", "skip"),
    ]
    idx = radiolist("Configure vision backend:", choices, default=2)
    chosen = choices[idx].value
    if chosen == "skip":
        print("  Skipped - add later with `oc setup` or configure vision settings.")
        return SectionResult.SKIPPED_FRESH

    vision = ctx.config.setdefault("vision", {})
    if chosen == "openrouter":
        vision.update({
            "provider": "openrouter",
            "model": "google/gemini-2.5-flash-preview",
        })
    else:
        vision.update({"provider": "custom"})
    print(f"  ✓ Vision backend set to {vision['provider']}")
    return SectionResult.CONFIGURED

"""Messaging platforms section handler.

Two-step Hermes flow:
  1. radiolist "Connect a messaging platform?" → [Set up now / Skip]
  2. checklist "Select platforms to configure:" → list of channel-kind plugins
"""
from __future__ import annotations

from typing import Any

from opencomputer.cli_setup.sections import SectionResult, WizardCtx
from opencomputer.cli_ui.menu import Choice, checklist, radiolist


def is_messaging_platforms_configured(ctx: WizardCtx) -> bool:
    gw = ctx.config.get("gateway") or {}
    return bool(gw.get("platforms"))


def _discover_platforms() -> list[dict[str, Any]]:
    """Return list of {'name', 'label', 'configured'} for each channel-kind
    plugin discovered via opencomputer.plugins.discovery."""
    try:
        from opencomputer.plugins.discovery import discover, standard_search_paths
        candidates = discover(standard_search_paths())
    except Exception:  # noqa: BLE001
        return []
    out: list[dict[str, Any]] = []
    for cand in candidates:
        kind = getattr(cand.manifest, "kind", None)
        if kind not in ("channel", "mixed"):
            continue
        name = cand.manifest.id
        label = getattr(cand.manifest, "label", None) or name.title()
        out.append({
            "name": name,
            "label": label,
            "configured": False,  # detailed check left to follow-up
        })
    return out


def _invoke_platform_setup(name: str, ctx: WizardCtx) -> bool:
    """Record the platform name in config.gateway.platforms.

    Per-platform credential prompts (Telegram bot token, Discord token,
    etc.) are deferred to subproject S5 / M2."""
    ctx.config.setdefault("gateway", {})
    ctx.config["gateway"].setdefault("platforms", [])
    if name not in ctx.config["gateway"]["platforms"]:
        ctx.config["gateway"]["platforms"].append(name)
    return True


def run_messaging_platforms_section(ctx: WizardCtx) -> SectionResult:
    gate_choices = [
        Choice("Set up messaging now (recommended)", "now"),
        Choice("Skip — set up later with `oc setup gateway`", "skip"),
    ]
    gate_idx = radiolist(
        "Connect a messaging platform? (Telegram, Discord, etc.)",
        gate_choices, default=0,
    )
    if gate_idx == 1:
        return SectionResult.SKIPPED_FRESH

    platforms = _discover_platforms()
    if not platforms:
        return SectionResult.SKIPPED_FRESH

    items = [
        Choice(
            label=p["label"],
            value=p["name"],
            description="(configured)" if p["configured"] else "(not configured)",
        )
        for p in platforms
    ]
    selected = checklist("Select platforms to configure:", items)
    if not selected:
        return SectionResult.SKIPPED_FRESH

    for idx in selected:
        _invoke_platform_setup(platforms[idx]["name"], ctx)

    return SectionResult.CONFIGURED

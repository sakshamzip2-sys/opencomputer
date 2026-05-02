"""Messaging platforms section handler.

Two-step Hermes flow:
  1. radiolist "Connect a messaging platform?" → [Set up now / Skip]
  2. checklist "Select platforms to configure:" → list of channel-kind plugins

For each selected platform, walks the channel manifest's env_vars and
prompts the user (use existing / enter new / skip) — same shape as the
inference_provider API-key entry. Keys land in ~/.opencomputer/.env
with 0600 perms.
"""
from __future__ import annotations

import getpass
from typing import Any

from opencomputer.cli_setup.env_writer import (
    default_env_file,
    read_env_value,
    write_env_value,
)
from opencomputer.cli_setup.sections import SectionResult, WizardCtx
from opencomputer.cli_ui.menu import Choice, checklist, radiolist


def is_messaging_platforms_configured(ctx: WizardCtx) -> bool:
    gw = ctx.config.get("gateway") or {}
    return bool(gw.get("platforms"))


def _discover_platforms() -> list[dict[str, Any]]:
    """Return list of {'name', 'label', 'env_vars', 'signup_url'} for
    each channel-kind plugin discovered via opencomputer.plugins.discovery."""
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
        setup = cand.manifest.setup
        if setup is None:
            continue
        channels = getattr(setup, "channels", []) or []
        for ch in channels:
            out.append({
                "name": ch.id,
                "label": getattr(ch, "label", "") or ch.id.title(),
                "env_vars": list(ch.env_vars or []),
                "signup_url": getattr(ch, "signup_url", "") or "",
            })
    return out


def _prompt_secret(env_var: str) -> str | None:
    """Prompt user for a secret (masked input). Returns the value or
    None if user submits empty / hits EOF."""
    print(f"  Enter {env_var} (input hidden, leave blank to skip):")
    try:
        value = getpass.getpass("    ")
    except (EOFError, KeyboardInterrupt):
        return None
    value = value.strip()
    return value or None


def _collect_secret(env_var: str, signup_url: str = "") -> str | None:
    """If env var already set: 3-option radiolist; else direct prompt.
    Returns the value to save or None for no-write."""
    existing = read_env_value(env_var)
    if existing:
        masked = f"…{existing[-4:]}" if len(existing) >= 4 else "(short)"
        choices = [
            Choice(f"Use existing {env_var} ({masked})", "use"),
            Choice("Enter a new value", "new"),
            Choice("Skip — leave as-is", "skip"),
        ]
        idx = radiolist(
            f"{env_var} is already set — what would you like to do?",
            choices, default=0,
        )
        if idx in (0, 2):
            return None
        return _prompt_secret(env_var)

    if signup_url:
        print(f"  Get a token at: {signup_url}")
    return _prompt_secret(env_var)


def _invoke_platform_setup(name: str, ctx: WizardCtx) -> bool:
    """Look up the platform's manifest, walk env_vars, prompt + save
    each via env_writer. Records platform name in config.gateway.platforms."""
    platforms = _discover_platforms()
    match = next((p for p in platforms if p["name"] == name), None)
    if match is None:
        ctx.config.setdefault("gateway", {})
        ctx.config["gateway"].setdefault("platforms", [])
        if name not in ctx.config["gateway"]["platforms"]:
            ctx.config["gateway"]["platforms"].append(name)
        return True

    print(f"\n◆ {match['label']}")
    if match["signup_url"]:
        print(f"  Setup guide: {match['signup_url']}")

    for env_var in match["env_vars"]:
        new_value = _collect_secret(env_var, match["signup_url"])
        if new_value:
            try:
                write_env_value(env_var, new_value)
                print(f"  ✓ Saved {env_var} to {default_env_file()}")
            except Exception as e:  # noqa: BLE001
                print(f"  ⚠ Could not save {env_var}: {type(e).__name__}: {e}")

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
            description="(not configured)",
        )
        for p in platforms
    ]
    selected = checklist("Select platforms to configure:", items)
    if not selected:
        return SectionResult.SKIPPED_FRESH

    for idx in selected:
        _invoke_platform_setup(platforms[idx]["name"], ctx)

    return SectionResult.CONFIGURED

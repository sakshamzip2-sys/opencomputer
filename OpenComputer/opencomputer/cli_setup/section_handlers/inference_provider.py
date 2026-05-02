"""Inference provider section handler.

Discovers provider plugins via opencomputer.plugins.discovery; lets
user pick one via radiolist; invokes the chosen plugin's setup hook.
"""
from __future__ import annotations

from typing import Any

from opencomputer.cli_setup.sections import SectionResult, WizardCtx
from opencomputer.cli_ui.menu import Choice, radiolist


def is_inference_provider_configured(ctx: WizardCtx) -> bool:
    model = ctx.config.get("model") or {}
    provider = model.get("provider")
    return bool(provider) and provider != "none"


def _discover_providers() -> list[dict[str, Any]]:
    """Return list of {'name', 'label', 'description'} for every provider
    plugin's manifest.setup.providers entry."""
    try:
        from opencomputer.plugins.discovery import discover, standard_search_paths
        candidates = discover(standard_search_paths())
    except Exception:  # noqa: BLE001
        return []
    out: list[dict[str, Any]] = []
    for cand in candidates:
        setup = cand.manifest.setup
        if setup is None:
            continue
        for prov in setup.providers:
            out.append({
                "name": prov.name,
                "label": getattr(prov, "label", "") or prov.name.title(),
                "description": getattr(prov, "description", "") or "",
            })
    return out


def _invoke_provider_setup(name: str, ctx: WizardCtx) -> bool:
    """Update config with the chosen provider name + primary env var.
    Real per-provider credential prompts are deferred to P1+ subprojects."""
    try:
        from opencomputer.plugins.discovery import discover, standard_search_paths
        for cand in discover(standard_search_paths()):
            setup = cand.manifest.setup
            if setup is None:
                continue
            for prov in setup.providers:
                if prov.name == name:
                    env_var = (prov.env_vars or [None])[0]
                    ctx.config.setdefault("model", {})
                    ctx.config["model"]["provider"] = name
                    if env_var:
                        ctx.config["model"]["api_key_env"] = env_var
                    return True
    except Exception:  # noqa: BLE001
        pass
    # Fallback minimum config write
    ctx.config.setdefault("model", {})
    ctx.config["model"]["provider"] = name
    return True


def run_inference_provider_section(ctx: WizardCtx) -> SectionResult:
    providers = _discover_providers()
    choices: list[Choice] = []
    for p in providers:
        choices.append(Choice(
            label=p["label"], value=p["name"],
            description=p["description"] or None,
        ))
    choices.append(Choice(
        label="Custom endpoint (enter URL manually)", value="__custom__",
        description="Manually configure base_url + api_key_env",
    ))
    choices.append(Choice(label="Leave unchanged", value="__leave__"))

    idx = radiolist("Select provider:", choices, default=0)
    chosen = choices[idx].value

    if chosen == "__leave__":
        return SectionResult.SKIPPED_KEEP

    if chosen == "__custom__":
        ctx.config.setdefault("model", {})
        ctx.config["model"]["provider"] = "custom"
        return SectionResult.CONFIGURED

    ok = _invoke_provider_setup(str(chosen), ctx)
    return SectionResult.CONFIGURED if ok else SectionResult.SKIPPED_FRESH

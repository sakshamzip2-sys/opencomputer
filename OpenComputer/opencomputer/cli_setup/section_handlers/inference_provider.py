"""Inference provider section handler.

Discovers provider plugins via opencomputer.plugins.discovery; lets
user pick one via radiolist; collects the API key (use existing /
re-enter / skip), saves to ~/.opencomputer/.env, updates config.
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
from opencomputer.cli_ui.menu import Choice, radiolist


def is_inference_provider_configured(ctx: WizardCtx) -> bool:
    model = ctx.config.get("model") or {}
    provider = model.get("provider")
    return bool(provider) and provider != "none"


def _discover_providers() -> list[dict[str, Any]]:
    """Return list of {'name', 'label', 'description', 'env_var', 'signup_url'}
    for every provider plugin's manifest.setup.providers entry."""
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
            env_vars = list(prov.env_vars or [])
            out.append({
                "name": prov.id,
                "label": getattr(prov, "label", "") or prov.id.title(),
                "description": getattr(prov, "description", "") or "",
                "env_var": env_vars[0] if env_vars else None,
                "signup_url": getattr(prov, "signup_url", "") or "",
            })
    return out


def _prompt_api_key(env_var: str, signup_url: str = "") -> str | None:
    """Prompt user for an API key (masked input). Returns the key or
    None if user submits empty / hits EOF."""
    if signup_url:
        print(f"  Get a key at: {signup_url}")
    print(f"  Enter {env_var} (input hidden, leave blank to skip):")
    try:
        value = getpass.getpass("    ")
    except (EOFError, KeyboardInterrupt):
        return None
    value = value.strip()
    return value or None


def _collect_api_key(env_var: str, signup_url: str) -> str | None:
    """Decide whether to use existing, re-enter, or skip the API key.
    Returns the key value to save (or None if no save needed).

    If env var already set (in shell or .env), 3-option radiolist:
      - Use existing → returns None (no save needed)
      - Enter new → prompt
      - Skip → returns None
    Otherwise: direct prompt; user-empty input → None (skipped)
    """
    existing = read_env_value(env_var)
    if existing:
        masked = f"…{existing[-4:]}" if len(existing) >= 4 else "(short)"
        choices = [
            Choice(f"Use existing {env_var} ({masked})", "use"),
            Choice("Enter a new key", "new"),
            Choice("Skip — leave key as-is", "skip"),
        ]
        idx = radiolist(
            f"{env_var} is already set — what would you like to do?",
            choices, default=0,
        )
        if idx == 0 or idx == 2:
            return None  # use-existing or skip — no write
        return _prompt_api_key(env_var, signup_url)

    return _prompt_api_key(env_var, signup_url)


def _test_provider_connection(provider_name: str, env_var: str) -> bool:
    """Polish: try to instantiate the provider with the new key. Just
    constructs the class — doesn't make a network call (those need
    real keys + network access not always available at setup time).

    Returns True if construction succeeded, False otherwise. Prints
    a hint either way. Best-effort — failure doesn't block the wizard.
    """
    import os

    if not os.environ.get(env_var):
        # Reload from .env in case the user just saved it. env_writer
        # writes the file but doesn't mutate os.environ — so a fresh
        # read is needed.
        from opencomputer.cli_setup.env_writer import read_env_value
        value = read_env_value(env_var)
        if value:
            os.environ[env_var] = value

    try:
        from opencomputer.plugins.discovery import discover, standard_search_paths
        from opencomputer.plugins.loader import load_plugin
        from opencomputer.plugins.registry import PluginRegistry
        # Discover the plugin and look up its provider class
        for cand in discover(standard_search_paths()):
            if cand.manifest.kind not in ("provider", "mixed"):
                continue
            # Heuristic: look at setup.providers to find a match
            setup = cand.manifest.setup
            if setup is None:
                continue
            for prov in setup.providers:
                if prov.id == provider_name:
                    # Try to construct the registered class
                    reg = PluginRegistry()
                    load_plugin(cand, reg)
                    cls = reg.get_provider(provider_name)
                    if cls is None:
                        return False
                    cls()  # raises if env var missing or other init issue
                    print(f"  ✓ Provider '{provider_name}' constructed successfully")
                    return True
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠ Could not instantiate provider: {type(e).__name__}: {e}")
        return False
    return False


def _invoke_provider_setup(name: str, ctx: WizardCtx) -> bool:
    """Update config with chosen provider, prompt for API key, save to
    ~/.opencomputer/.env, optionally test the provider. Returns True on success."""
    providers = _discover_providers()
    match = next((p for p in providers if p["name"] == name), None)
    if match is None:
        # Unknown provider — minimal config write, no key prompt
        ctx.config.setdefault("model", {})
        ctx.config["model"]["provider"] = name
        return True

    env_var = match["env_var"]
    ctx.config.setdefault("model", {})
    ctx.config["model"]["provider"] = name
    if env_var:
        ctx.config["model"]["api_key_env"] = env_var
        new_key = _collect_api_key(env_var, match["signup_url"])
        if new_key:
            try:
                write_env_value(env_var, new_key)
                print(f"  ✓ Saved {env_var} to {default_env_file()}")
            except Exception as e:  # noqa: BLE001
                print(f"  ⚠ Could not save key: {type(e).__name__}: {e}")
                print(f"    Set {env_var} in your shell to use this provider.")
        # Polish: try to construct the provider class to catch obvious
        # issues (wrong key shape, missing dependency). Best-effort.
        _test_provider_connection(name, env_var)
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

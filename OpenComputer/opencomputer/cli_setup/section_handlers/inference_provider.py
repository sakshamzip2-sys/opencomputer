"""Inference provider section handler.

Discovers provider plugins via opencomputer.plugins.discovery; lets
user pick one via radiolist; collects the API key (use existing /
re-enter / skip), saves to ~/.opencomputer/.env, updates config.
"""
from __future__ import annotations

import getpass
import os
from typing import Any

from opencomputer.cli_setup.env_writer import (
    default_env_file,
    read_env_value,
    write_env_value,
)
from opencomputer.cli_setup.sections import SectionResult, WizardCtx
from opencomputer.cli_ui.menu import Choice, radiolist
from opencomputer.openrouter_catalog import (
    OPENROUTER_MODEL_IDS,
    display_model_ids,
    fetch_openrouter_models,
    setup_model_ids,
)

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_DESCRIPTION = "100+ models, pay-per-use, free"
OPENROUTER_RECOMMENDED_MODELS = OPENROUTER_MODEL_IDS
OPENROUTER_FALLBACK_FREE_MODELS = OPENROUTER_MODEL_IDS
OPENROUTER_MODEL_LIMIT = 500


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
            provider_id = prov.id
            description = getattr(prov, "description", "") or ""
            default_model = getattr(prov, "default_model", "") or ""
            if provider_id == "openrouter":
                description = description or OPENROUTER_DESCRIPTION
                default_model = default_model or OPENROUTER_RECOMMENDED_MODELS[0]
            out.append({
                "name": provider_id,
                "label": getattr(prov, "label", "") or prov.id.title(),
                "description": description,
                "env_var": env_vars[0] if env_vars else None,
                "default_model": default_model,
                "signup_url": getattr(prov, "signup_url", "") or "",
            })
    out.sort(key=lambda p: 0 if p["name"] == "openrouter" else 1)
    return out


def _openrouter_api_key() -> str:
    return os.environ.get("OPENROUTER_API_KEY") or read_env_value("OPENROUTER_API_KEY") or ""


def _env_value_present(name: str) -> bool:
    return bool(os.environ.get(name) or read_env_value(name))


def _openrouter_base_url() -> str:
    return os.environ.get("OPENROUTER_BASE_URL") or read_env_value("OPENROUTER_BASE_URL") or OPENROUTER_BASE_URL


def _curate_openrouter_model_ids(model_ids: list[str]) -> list[str]:
    """Return the screenshot-pinned OpenRouter menu order."""
    return display_model_ids()


def _fetch_openrouter_models(
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    limit: int = OPENROUTER_MODEL_LIMIT,
) -> list[str]:
    """Fetch current OpenRouter models and cache their context windows.

    OpenRouter's catalog changes frequently, so setup asks the live
    ``/models`` endpoint when possible and falls back to a small known-free
    list if offline.
    """
    rows = fetch_openrouter_models(
        api_key=api_key or _openrouter_api_key(),
        base_url=base_url or _openrouter_base_url(),
        limit=limit,
    )
    return [row.model_id for row in rows[:limit]] or display_model_ids()[:limit]


def _is_openrouter_model_id(model_id: str) -> bool:
    return "/" in model_id


def _is_google_openrouter_model(model_id: str) -> bool:
    return (model_id or "").strip().lower().startswith("google/")


def _model_matches_provider(provider: str, model_id: str) -> bool:
    """Best-effort guard against persisting an incompatible model id."""
    m = (model_id or "").strip().lower()
    if not m:
        return False
    if provider == "openrouter":
        return "/" in m
    if provider == "openai":
        return "/" not in m and m.startswith((
            "gpt-", "o1", "o2", "o3", "o4", "o5", "o6", "chatgpt",
        ))
    if provider == "anthropic":
        return "/" not in m and m.startswith("claude")
    if provider in {"gemini", "google"}:
        return "/" not in m and m.startswith(("gemini", "palm"))
    if provider == "deepseek":
        return "/" not in m and m.startswith("deepseek")
    if provider == "minimax":
        return "/" not in m and m.startswith("minimax")
    if provider == "kimi":
        return "/" not in m and m.startswith(("kimi", "moonshot"))
    if provider == "qwen":
        return "/" not in m and m.startswith("qwen")
    return True


def _choose_openrouter_model(ctx: WizardCtx, *, default_model: str) -> str | None:
    current = str((ctx.config.get("model") or {}).get("model") or "")
    _fetch_openrouter_models()
    models = setup_model_ids(current)

    # Escape hatches go at the TOP so they're always visible — radiolist
    # has no scrollbar, and OpenRouter's catalog overflows most terminals,
    # which would otherwise push these off-screen.
    choices: list[Choice] = [
        Choice("Enter custom model name", "__custom__"),
        Choice("Skip (keep current)", "__skip__"),
    ]
    current_idx = -1
    for model_id in models:
        label = model_id
        if model_id == current:
            label = f"{model_id}  ← currently in use"
            current_idx = len(choices)
        choices.append(Choice(label, model_id))

    if (
        current
        and current_idx == -1
        and _is_openrouter_model_id(current)
        and not _is_google_openrouter_model(current)
    ):
        current_idx = len(choices)
        choices.append(Choice(f"{current}  ← currently in use", current))

    # Cursor starts on the currently-used model (or first real model)
    # so pressing Enter without scrolling keeps the previous behaviour.
    default_idx = current_idx if current_idx >= 0 else min(2, len(choices) - 1)
    idx = radiolist("Select default OpenRouter model:", choices, default=default_idx)
    chosen = choices[idx].value
    if chosen == "__skip__":
        return None
    if chosen == "__custom__":
        try:
            raw = input("OpenRouter model id: ").strip()
        except (EOFError, KeyboardInterrupt):
            return None
        return raw or None
    return str(chosen)


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
                    api = reg.api()
                    load_plugin(cand, api)
                    registered = reg.providers.get(provider_name)
                    if registered is None:
                        return False
                    if isinstance(registered, type):
                        registered()  # raises if env var missing or other init issue
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
    default_model = match.get("default_model") or ""
    ctx.config.setdefault("model", {})
    model_cfg = ctx.config["model"]
    previous_provider = str(model_cfg.get("provider") or "")
    previous_model = str(model_cfg.get("model") or "")
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
    if name == "openrouter":
        try:
            if not _env_value_present("OPENROUTER_BASE_URL"):
                write_env_value("OPENROUTER_BASE_URL", OPENROUTER_BASE_URL)
        except Exception:  # noqa: BLE001
            pass
        chosen_model = _choose_openrouter_model(
            ctx,
            default_model=default_model or OPENROUTER_FALLBACK_FREE_MODELS[0],
        )
        if chosen_model:
            ctx.config["model"]["model"] = chosen_model
    elif default_model:
        if (
            not previous_model
            or previous_provider != name
            or not _model_matches_provider(name, previous_model)
        ):
            ctx.config["model"]["model"] = default_model
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

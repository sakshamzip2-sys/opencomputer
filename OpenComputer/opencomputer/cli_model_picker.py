"""``oc model`` — interactive model + provider picker.

Uses the prompt_toolkit ``radiolist`` from :mod:`opencomputer.cli_ui.menu`
so users can navigate with arrow keys, jump by typing a number, ENTER /
SPACE to select, ESC to cancel. Matches the Hermes-style provider-pick UI
(radio glyphs + "← currently active" markers).

Provider list comes from :func:`_discover_providers` (every plugin
manifest's ``setup.providers`` entry — 30+ providers). Model list
comes from the in-memory model_metadata registry, filtered to the
chosen provider; unknown providers fall back to a free-text prompt.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

import typer
from rich.console import Console

from opencomputer.agent.config_store import (
    load_config,
    save_config,
    set_value,
)
from opencomputer.agent.model_metadata import list_models
from opencomputer.cli_ui.menu import Choice, WizardCancelled, radiolist
from opencomputer.openrouter_catalog import display_model_ids, fetch_openrouter_models

console = Console()


# Vendor prefixes that appear in OpenRouter model ids (``<vendor>/<model>``).
# Used by :func:`_infer_provider` to route slash-separated ids to OpenRouter
# rather than mis-classifying them as OpenAI. Keep this short — it's only
# an inference fallback for entries lacking an explicit ``provider_id``.
_OPENROUTER_VENDOR_PREFIXES = (
    "anthropic/", "openai/", "google/", "meta-llama/", "mistralai/",
    "qwen/", "moonshotai/", "minimax/", "deepseek/", "x-ai/",
    "nousresearch/", "perplexity/", "cohere/", "01-ai/", "nvidia/",
    "microsoft/", "ai21/", "amazon/",
)


def _infer_provider(model_id: str) -> str:
    """Map a model id to its provider when the registry entry has none."""
    m = model_id.lower()
    # OpenRouter ids are ``<vendor>/<model>[:tag]`` — match before bare prefixes
    # so ``minimax/minimax-m2.5:free`` doesn't get classified by ``minimax``
    # rather than openrouter.
    if "/" in m and m.startswith(_OPENROUTER_VENDOR_PREFIXES):
        return "openrouter"
    if m.endswith(":free") and "/" in m:
        return "openrouter"
    if m.startswith("claude"):
        return "anthropic"
    if m.startswith(("gpt", "o1", "o2", "o3", "o4", "o5", "o6", "chatgpt")):
        return "openai"
    if m.startswith(("gemini", "palm")):
        return "google"
    if m.startswith("llama"):
        return "meta"
    if m.startswith(("mixtral", "mistral", "codestral")):
        return "mistral"
    if m.startswith("deepseek"):
        return "deepseek"
    if m.startswith(("groq", "kimi")):
        return "groq"
    return "unknown"


def _grouped_models() -> dict[str, list[str]]:
    """Return ``{provider_id: [model_id, ...]}`` from the in-memory registry."""
    grouped: dict[str, list[str]] = defaultdict(list)
    for entry in list_models():
        if not entry.model_id:
            continue
        provider = entry.provider_id or _infer_provider(entry.model_id)
        grouped[provider].append(entry.model_id)
    return {p: sorted(set(grouped[p])) for p in sorted(grouped.keys())}


def _provider_label(provider: str) -> str:
    """Render a human label for a provider id (legacy helper, used in success message)."""
    pretty = {
        "openai":     "OpenAI",
        "anthropic":  "Anthropic",
        "google":     "Google",
        "groq":       "Groq",
        "openrouter": "OpenRouter",
        "deepseek":   "DeepSeek",
        "mistral":    "Mistral",
        "meta":       "Meta",
        "unknown":    "Other",
    }
    return pretty.get(provider, provider.capitalize())


def _discover_provider_rows() -> list[dict[str, Any]]:
    """Return every plugin manifest's ``setup.providers`` entry."""
    try:
        from opencomputer.cli_setup.section_handlers.inference_provider import (
            _discover_providers,
        )
        return _discover_providers()
    except Exception:  # noqa: BLE001 — picker still works without descriptions
        return []


def _label_with_marker(name: str, *, marker: str) -> str:
    """Back-compat helper retained for tests/callers."""
    return f"{name}  ← {marker}"


def _pick_provider(
    rows: list[dict[str, Any]],
    current: str,
) -> str | None:
    """Provider step. Returns selected provider id or None on cancel."""
    choices: list[Choice] = []
    default_idx = 0
    for i, row in enumerate(rows):
        name = row["name"]
        label = row.get("label") or name.title()
        description = row.get("description") or ""
        if name == current:
            label = f"{label}  ← currently active"
            default_idx = i
        choices.append(Choice(label=label, value=name, description=description))

    try:
        idx = radiolist("Select provider:", choices, default=default_idx)
    except WizardCancelled:
        return None
    return str(choices[idx].value)


def _pick_model(
    models: list[str],
    current: str,
    *,
    allow_custom: bool = True,
) -> str | None:
    """Model step. Returns selected model id (or typed-in custom id) or None.

    When ``models`` is empty (provider has no preregistered models in the
    curated catalog), prompts directly for a custom id. When non-empty
    and ``allow_custom`` is True, appends custom and skip options so users
    can type a model name not in the curated list (e.g. OpenRouter has
    thousands) or keep the current model.
    """
    if not models:
        return _prompt_custom_model(current)

    choices: list[Choice] = []
    # Escape hatches go at the TOP so they remain visible even when the
    # catalog overflows the terminal (radiolist has no scroll indicator).
    if allow_custom:
        choices.append(Choice(label="Enter custom model name", value="__custom__"))
        choices.append(Choice(label="Skip (keep current)", value="__skip__"))

    default_idx = len(choices)  # first real model
    for m in models:
        label = m
        if m == current:
            label = f"{m}  ← currently in use"
            default_idx = len(choices)
        choices.append(Choice(label=label, value=m))

    try:
        idx = radiolist("Select a model:", choices, default=default_idx)
    except WizardCancelled:
        return None
    chosen = choices[idx].value
    if chosen == "__skip__":
        return None
    if chosen == "__custom__":
        return _prompt_custom_model(current)
    return str(chosen)


def _prompt_custom_model(current: str) -> str | None:
    """Free-text fallback for providers without curated catalog entries."""
    console.print(
        "\n[dim]No curated models for this provider. Type a model id "
        "(e.g. [cyan]anthropic/claude-opus-4.7[/cyan] for OpenRouter) or "
        "press Enter to cancel.[/dim]"
    )
    if current:
        console.print(f"  Current: [cyan]{current}[/cyan]")
    try:
        raw = input("  Model id: ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    return raw or None


def _models_for_provider(provider: str, grouped: dict[str, list[str]]) -> list[str]:
    if provider == "openrouter":
        try:
            fetch_openrouter_models()
        except Exception:  # noqa: BLE001
            pass
        return display_model_ids()
    return grouped.get(provider, [])


def model_picker() -> None:
    """Interactive provider + model picker. Persists to active config.yaml."""
    # Load user-curated overrides into the in-memory registry so models
    # added via ``oc models add`` are visible here. Without this, the
    # picker only sees the curated default catalog from module load.
    try:
        from opencomputer.cli import _apply_model_overrides
        _apply_model_overrides()
    except Exception:  # noqa: BLE001 — overrides are best-effort
        pass

    rows = _discover_provider_rows()
    grouped = _grouped_models()

    # Merge: every discovered plugin provider, plus any model-registry
    # provider we don't already cover (so curated catalog entries with
    # no plugin still surface).
    seen = {r["name"] for r in rows}
    for prov in grouped:
        if prov in seen:
            continue
        rows.append({
            "name": prov,
            "label": _provider_label(prov),
            "description": "",
            "default_model": "",
        })

    if not rows:
        console.print(
            "[yellow](._.) No providers available.[/yellow] "
            "Install a provider plugin first."
        )
        raise typer.Exit(1)

    cfg = load_config()
    current_p = cfg.model.provider
    current_m = cfg.model.model

    console.print(f"Current model:    [cyan]{current_m}[/cyan]")
    console.print(f"Active provider:  [cyan]{current_p}[/cyan]\n")

    chosen_provider = _pick_provider(rows, current_p)
    if chosen_provider is None:
        console.print("\nNo change.")
        raise typer.Exit(0)

    # Models for the chosen provider come from the registry. If empty
    # (most plugin providers don't ship curated metadata), `_pick_model`
    # falls through to a free-text prompt.
    models = _models_for_provider(chosen_provider, grouped)
    # If we know the plugin's default_model and it's not in the catalog,
    # surface it so users have something to pick.
    row = next((r for r in rows if r["name"] == chosen_provider), None)
    default_model = (row or {}).get("default_model") or ""
    if default_model and default_model not in models:
        models = [default_model, *models]

    chosen_model = _pick_model(models, current_m)
    if chosen_model is None:
        console.print("\nNo change.")
        raise typer.Exit(0)

    new_cfg = set_value(cfg, "model.provider", chosen_provider)
    new_cfg = set_value(new_cfg, "model.model", chosen_model)
    save_config(new_cfg)

    console.print(
        f"\n[green]✓[/green] Default model set to: [cyan]{chosen_model}[/cyan] "
        f"(via {_provider_label(chosen_provider)})"
    )


__all__ = [
    "model_picker",
    "_grouped_models",
    "_infer_provider",
    "_pick_provider",
    "_pick_model",
    "_provider_label",
    "_label_with_marker",
]

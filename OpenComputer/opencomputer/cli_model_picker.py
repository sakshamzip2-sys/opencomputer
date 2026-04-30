"""``oc model`` — interactive model + provider picker (Hermes-exact UX).

Three-tier picker fallback (TerminalMenu → curses → numbered list) ported
from Hermes' ``hermes model`` flow. Active provider/model are pre-marked
with ``← currently active`` / ``← currently in use`` suffixes; ESC/q
returns ``Leave unchanged.`` without writing.

History:
- 2026-04-30 (PR #275): initial Hermes-parity port, but used numbered
  ``typer.prompt`` only and dropped models with empty ``provider_id``.
- 2026-04-30 (PR #276): added ``_infer_provider`` so curated models surface.
- 2026-04-30 (this commit): full UX-parity with Hermes — TerminalMenu
  arrow-key picker, active markers, "Leave unchanged" sentinel, exact
  output strings.
"""
from __future__ import annotations

from collections import defaultdict

import typer
from rich.console import Console

from opencomputer.agent.config_store import (
    load_config,
    save_config,
    set_value,
)
from opencomputer.agent.model_metadata import list_models
from opencomputer.cli_ui.term_menu import pick_one

console = Console()


_LEAVE_UNCHANGED = "Leave unchanged"


def _infer_provider(model_id: str) -> str:
    """Map a model id to its provider when the registry entry has none.

    The G.32 curated catalog ships every entry with ``provider_id=None``.
    Without inference, the picker would show no models. Falls back to
    ``"unknown"`` so unrecognised prefixes still appear (under their own
    bucket) instead of being silently dropped.
    """
    m = model_id.lower()
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


def _label_with_marker(name: str, *, marker: str) -> str:
    """Render ``<name>  ← <marker>`` so the active row is visible at a glance.

    Hermes uses two-space pad + arrow + literal marker text.
    """
    return f"{name}  ← {marker}"


def _pick_provider(grouped: dict[str, list[str]], current: str) -> str | None:
    """Provider step. Returns selected provider id or None on cancel."""
    providers = list(grouped.keys())
    labels: list[str] = []
    current_idx = 0
    for i, p in enumerate(providers):
        if p == current:
            labels.append(_label_with_marker(p, marker="currently active"))
            current_idx = i
        else:
            labels.append(p)
    labels.append(_LEAVE_UNCHANGED)
    idx = pick_one(
        title="Select a provider:",
        choices=labels,
        current_idx=current_idx,
        allow_cancel=True,
    )
    if idx is None or idx == len(labels) - 1:
        return None
    return providers[idx]


def _pick_model(models: list[str], current: str) -> str | None:
    """Model step. Returns selected model id or None on cancel."""
    labels: list[str] = []
    current_idx = 0
    for i, m in enumerate(models):
        if m == current:
            labels.append(_label_with_marker(m, marker="currently in use"))
            current_idx = i
        else:
            labels.append(m)
    labels.append(_LEAVE_UNCHANGED)
    idx = pick_one(
        title="Select a model:",
        choices=labels,
        current_idx=current_idx,
        allow_cancel=True,
    )
    if idx is None or idx == len(labels) - 1:
        return None
    return models[idx]


def _provider_label(provider: str) -> str:
    """Render a human label for the success message — Hermes uses
    capitalised provider names (``OpenAI``, ``Anthropic``, etc).
    """
    pretty = {
        "openai":     "OpenAI",
        "anthropic":  "Anthropic",
        "google":     "Google",
        "groq":       "Groq",
        "openrouter": "OpenRouter",
        "deepseek":   "DeepSeek",
        "mistral":    "Mistral",
        "meta":       "Meta",
    }
    return pretty.get(provider, provider.capitalize())


def model_picker() -> None:
    """Interactive provider + model picker. Persists to active config.yaml.

    Hermes-exact output (matches ``hermes_cli/main.py:1469-1471`` +
    ``hermes_cli/auth.py:2818``)::

        Current model: <id>
        Active provider: <id>

        Select a provider:
        [arrow-key picker]

        Select a model:
        [arrow-key picker]

        Default model set to: <id> (via <ProviderLabel>)
    """
    grouped = _grouped_models()
    if not grouped:
        console.print(
            "[yellow](._.) No models registered yet.[/yellow]\n"
            "Add one first with [cyan]oc models add <provider> <model>[/cyan]."
        )
        raise typer.Exit(1)

    cfg = load_config()
    current_p = cfg.model.provider
    current_m = cfg.model.model

    console.print(f"Current model: [cyan]{current_m}[/cyan]")
    console.print(f"Active provider: [cyan]{current_p}[/cyan]")
    console.print()

    chosen_provider = _pick_provider(grouped, current_p)
    if chosen_provider is None:
        console.print("No change.")
        raise typer.Exit(0)

    models = grouped[chosen_provider]
    if not models:
        console.print(
            f"[yellow](._.) No models registered under "
            f"'{chosen_provider}'.[/yellow]"
        )
        raise typer.Exit(1)

    chosen_model = _pick_model(models, current_m)
    if chosen_model is None:
        console.print("No change.")
        raise typer.Exit(0)

    new_cfg = set_value(cfg, "model.provider", chosen_provider)
    new_cfg = set_value(new_cfg, "model.model", chosen_model)
    save_config(new_cfg)

    console.print(
        f"\nDefault model set to: [cyan]{chosen_model}[/cyan] "
        f"(via {_provider_label(chosen_provider)})"
    )


__all__ = [
    "model_picker",
    "_grouped_models",
    "_infer_provider",
    "_pick_provider",
    "_pick_model",
    "_provider_label",
]

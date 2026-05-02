"""Azure AI Foundry provider plugin."""
from __future__ import annotations

from provider import AzureFoundryProvider  # type: ignore[import-not-found]


def register(api) -> None:  # PluginAPI duck-typed
    api.register_provider("azure-foundry", AzureFoundryProvider)

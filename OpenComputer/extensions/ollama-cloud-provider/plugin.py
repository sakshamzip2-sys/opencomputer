"""Ollama Cloud provider plugin — registers OllamaCloudProvider as 'ollama-cloud'."""
from __future__ import annotations

from provider import OllamaCloudProvider  # type: ignore[import-not-found]


def register(api) -> None:  # PluginAPI duck-typed
    api.register_provider("ollama-cloud", OllamaCloudProvider)

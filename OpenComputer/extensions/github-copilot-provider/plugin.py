"""GitHub Copilot provider plugin."""
from __future__ import annotations

from provider import GitHubCopilotProvider  # type: ignore[import-not-found]


def register(api) -> None:  # PluginAPI duck-typed
    api.register_provider("copilot", GitHubCopilotProvider)

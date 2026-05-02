"""GitHub Copilot ACP provider plugin entry."""
from __future__ import annotations

from provider import CopilotACPProvider  # type: ignore[import-not-found]


def register(api) -> None:
    api.register_provider("copilot-acp", CopilotACPProvider)

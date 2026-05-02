"""NVIDIA NIM provider plugin — registers NvidiaNIMProvider as 'nvidia'."""
from __future__ import annotations

from provider import NvidiaNIMProvider  # type: ignore[import-not-found]


def register(api) -> None:  # PluginAPI duck-typed
    api.register_provider("nvidia", NvidiaNIMProvider)

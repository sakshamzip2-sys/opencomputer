"""HuggingFace provider plugin — registers HuggingFaceProvider as 'huggingface'."""
from __future__ import annotations

from provider import HuggingFaceProvider  # type: ignore[import-not-found]


def register(api) -> None:  # PluginAPI duck-typed
    api.register_provider("huggingface", HuggingFaceProvider)
